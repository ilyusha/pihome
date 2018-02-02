import  RPi.GPIO as GPIO
from time        import time, sleep, strftime
from picamera    import PiCamera, PiCameraCircularIO
from twilio.rest import Client
from uuid        import uuid4
from email.mime.multipart import MIMEMultipart
from email.mime.text      import MIMEText
from email.mime.image     import MIMEImage
from email.mime.base      import MIMEBase
from email                import encoders
from subprocess           import call
from threading            import Timer
import smtplib
import boto3
import os
import argparse
import config

INPUT_PIN = 16
OUTPUT_PIN = 25
MAX_DELTA = 15
SLEEP_INTERVAL = 0.1

class RingHandler:
    def invoke(self, state_dict):
        raise NotImplementedError


class SmsHandler(RingHandler):
    def __init__(self, s3_client, twilio_client, camera, recipients):
        self.camera = camera
        self.twilio_client = twilio_client
        self.s3_client = s3_client
        self.recipients = recipients

    def upload_to_s3(self, img_path):
        s3_img_file = "%s.jpg"%(str(uuid4()),)
        s3_img_obj = self.s3_client.Object(config.S3_BUCKET, s3_img_file)
        s3_img_obj.put(Body=open(img_path, "rb"), ContentType='image/jpeg')
        s3_img_obj.Acl().put(ACL="public-read")
        img_url = "%s/%s/%s"%(config.S3_BASE, config.S3_BUCKET, s3_img_file)
        return img_url

    def send_knock_msg(self):
        for recip in self.recipients:
            self.twilio_client.messages.create(to=recip, from_=config.TWILIO_NUMBER, body="knock knock!")

    def send_snap_msg(self, img_url):
        for recip in self.recipients:
            self.twilio_client.messages.create(to=recip, from_=config.TWILIO_NUMBER, body="", media_url=img_url)

    def invoke(self, state_dict):
        filename = "capture_%s"%strftime("%Y%m%d-%H%M%S")
        img_path = os.path.join("captures", "%s.jpg"%filename)
        self.camera.capture(img_path)
        self.send_knock_msg()
        img_url = self.upload_to_s3(img_path)
        self.send_snap_msg(img_url)
        state_dict["file_base"] = filename
        state_dict["img"] = img_path

class LEDHandler(RingHandler):
    def __init__(self, output_pin):
        self.output_pin = output_pin

    def _led_on(self):
        GPIO.output(self.output_pin, GPIO.HIGH)

    def _led_off(self):
        GPIO.output(self.output_pin, GPIO.LOW)

    def invoke(self, state_dict):
        self._led_on()
        Timer(5, self._led_off).start()

class EmailHandler(RingHandler):
    def __init__(self, smtp, recipients, camera_stream=None, stream_seconds=0):
        self.smtp = smtp
        self.recipients = recipients
        self.camera_stream = camera_stream
        self.stream_seconds = stream_seconds

    def invoke(self, state_dict):
        img_path = state_dict.get("img")
        if self.camera_stream is not None:
            filename = state_dict.get("file_base")
            video_path = os.path.join("captures", "%s.mp4"%filename)
            self.save_video(video_path)
            state_dict["video"] = video_path
        else:
            video_path = None
        self.send_email(img_path, video_path)

    def save_video(self, video_path):
        tempfile = "temp.h264"
        self.camera_stream.copy_to(tempfile, seconds=self.stream_seconds)
        with open(os.devnull, 'w') as silent:
            call(["MP4Box", "-add", tempfile, video_path], stdout=silent, stderr=silent)
        os.remove(tempfile)

    def send_email(self, img_path, video_path):
        msg = MIMEMultipart()
        msg['From'] = config.EMAIL_ACCT
        msg['To'] = ", ".join(self.recipients)
        msg['Subject'] = "Knock Knock"
        msg.attach(MIMEText("Someone is at the door", 'plain'))
        image_file = open(img_path, "rb")
        img = MIMEImage(image_file.read())
        image_file.close()
        msg.attach(img)
        img.add_header('Content-ID', '<{}>'.format(img_path))
        if video_path is not None:
            video_file = open(video_path, "rb")
            video_part = MIMEBase("application", "octet-stream")
            video_part.set_payload(video_file.read())
            encoders.encode_base64(video_part)
            video_part.add_header("Content-Disposition", "attachment", filename=video_path)
            video_part.add_header("Content-Type", "video/mp4")
            video_file.close()
            msg.attach(video_part)
        self.smtp.sendmail(config.EMAIL_ACCT, list(self.recipients), msg.as_string())

class Doorbell:

    def __init__(self):
        self.handlers = []
        self.working = False

    def add_handler(self, handler):
        self.handlers.append(handler)

    def ring(self):
        if self.working:
            return
        self.working = True
        state = {}
        try:
            for handler in self.handlers:
                handler.invoke(state)
        finally:
            self.working = False

def make_callback(doorbell):
    last_timestamp = 0
    def callback(channel):
        nonlocal last_timestamp
        timestamp = time()
        if timestamp - last_timestamp > MAX_DELTA:
            last_timestamp = timestamp
            doorbell.ring()
    return callback

def setup_gpio(callback):
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(INPUT_PIN, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
    GPIO.setup(OUTPUT_PIN, GPIO.OUT)
    GPIO.add_event_detect(INPUT_PIN, GPIO.RISING, callback=callback)

def setup_email(account, password):
    server = smtplib.SMTP("smtp.gmail.com", 587)
    server.ehlo()
    server.starttls()
    server.login(account, password)
    return server

if __name__ == "__main__":
    doorbell = Doorbell()
    stream_seconds = 5
    try:
        print("creating camera")
        camera = PiCamera()
        stream = PiCameraCircularIO(camera, seconds=stream_seconds)
        camera.start_recording(stream, format='h264')
        print("creating twilio client")
        twilio_client = Client(config.TWILIO_SID, config.TWILIO_TOKEN)
        print("creating S3 client")
        s3 = boto3.resource("s3", aws_access_key_id=config.AWS_KEY_ID, aws_secret_access_key=config.AWS_SECRET)
        print("connecting to SMTP server")
        smtp = setup_email(config.EMAIL_ACCT, config.EMAIL_PASSWORD)
        doorbell.add_handler(LEDHandler(OUTPUT_PIN))
        doorbell.add_handler(SmsHandler(s3, twilio_client, camera, recipients=config.SMS_RECIPIENTS))
        doorbell.add_handler(EmailHandler(smtp, config.EMAIL_RECIPIENTS, camera_stream=stream, stream_seconds=5))
        callback = make_callback(doorbell)
        setup_gpio(callback)
        input("Press any key to exit")
    finally:
        smtp.quit()
        GPIO.cleanup()
        camera.close()
        stream.close()
