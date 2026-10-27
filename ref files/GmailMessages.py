from __future__ import print_function
import httplib2
import os
import datetime
import BoilerCalander
import json
import base64
import email
import mimetypes
import os
from datetime import timedelta
from apiclient import discovery
from oauth2client import client
from oauth2client import tools
from oauth2client.file import Storage
from email.mime.audio import MIMEAudio
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from apiclient import errors

try:
    import argparse

    flags = argparse.ArgumentParser(parents=[tools.argparser]).parse_args()
except ImportError:
    flags = None

SCOPES = "https://mail.google.com/"
# CLIENT_SECRET_FILE = 'client_secret.json'
APPLICATION_NAME = "Google Gmail API .NET"


class EmailMessage(object):
    Mailfrom = ""
    Mailto = ""
    subject = ""
    body = ""
    date = ""

    def __init__(self, mfrom="", to="", subject="", body="", date=""):
        if mfrom.find("<") >= 0:
            self.Mailfrom = (
                mfrom[mfrom.index("<") : len(mfrom)].replace("<", "").replace(">", "").strip()
            )
        else:
            self.Mailfrom = mfrom
        self.Mailto = to
        self.subject = subject
        self.body = body
        self.date = date


class GmailMessages(object):
    """description of class"""

    secretPath = ""

    def __init__(self, secret=""):
        self.secretPath = secret

    def get_credentials(self):
        """Gets valid user credentials from storage.
        If nothing has been stored, or if the stored credentials are invalid,
        the OAuth2 flow is completed to obtain the new credentials.
        Returns:
            Credentials, the obtained credential.
        """
        cur_dir = os.getcwd()
        credential_dir = os.path.join(cur_dir, ".credentials")
        if not os.path.exists(credential_dir):
            os.makedirs(credential_dir)
        credential_path = os.path.join(credential_dir, "Gmail-python-quickstart.json")

        store = Storage(credential_path)
        credentials = store.get()
        if not credentials or credentials.invalid:
            flow = client.flow_from_clientsecrets(self.secretPath, SCOPES)
            flow.user_agent = APPLICATION_NAME
            if flags:
                credentials = tools.run_flow(flow, store, flags)
            else:  # Needed only for compatibility with Python 2.6
                credentials = tools.run(flow, store)
            print("Storing credentials to " + credential_path)
        return credentials

    def SendMessage(self, MailSubect="", MailBody=0, MailTo=0, attachment=""):
        credentials = self.get_credentials()
        http = credentials.authorize(httplib2.Http())
        service = discovery.build("gmail", "v1", http=http)
        msg = self.create_message_with_attachment(
            "blabla@gmail.com", MailTo, MailSubect, MailBody, attachment
        )
        self.send_message(service, "Me", msg)

    def create_message_with_attachment(self, sender, to, subject, message_text, file=""):
        """Create a message for an email.

        Args:
          sender: Email address of the sender.
          to: Email address of the receiver.
          subject: The subject of the email message.
          message_text: The text of the email message.
          file: The path to the file to be attached.

        Returns:
          An object containing a base64url encoded email object.
        """
        message = MIMEMultipart()
        message["to"] = to
        message["from"] = sender
        message["subject"] = subject

        msg = MIMEText(message_text)
        message.attach(msg)

        if file != "":
            content_type, encoding = mimetypes.guess_type(file)

            if content_type is None or encoding is not None:
                content_type = "application/octet-stream"
            main_type, sub_type = content_type.split("/", 1)
            if main_type == "text":
                fp = open(file, "rb")
                msg = MIMEText(fp.read(), _subtype=sub_type)
                fp.close()
            elif main_type == "image":
                fp = open(file, "rb")
                msg = MIMEImage(fp.read(), _subtype=sub_type)
                fp.close()
            elif main_type == "audio":
                fp = open(file, "rb")
                msg = MIMEAudio(fp.read(), _subtype=sub_type)
                fp.close()
            else:
                fp = open(file, "rb")
                msg = MIMEBase(main_type, sub_type)
                msg.set_payload(fp.read())
                fp.close()
            filename = os.path.basename(file)
            msg.add_header("Content-Disposition", "attachment", filename=filename)
            message.attach(msg)
        # return {'raw': base64.urlsafe_b64encode(message.as_string())} # python .2.7
        return {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode()}  # python 3

    def send_message(self, service, user_id, message):
        """Send an email message.

        Args:
          service: Authorized Gmail API service instance.
          user_id: User's email address. The special value "me"
          can be used to indicate the authenticated user.
          message: Message to be sent.

        Returns:
          Sent Message.
        """
        try:
            message = service.users().messages().send(userId=user_id, body=message).execute()
            print("Message sent Id: " + str(message["id"]))
            return message
        except errors.HttpError as error:
            print("An error occurred: " + str(error))

    def ReadMessages(self, delete=True):
        credentials = self.get_credentials()
        http = credentials.authorize(httplib2.Http())
        service = discovery.build("gmail", "v1", http=http)
        messages = self.ListMessagesMatchingQuery(service, "me", "in:inbox", delete)
        messgelist = []
        for msg in messages:

            message = (
                service.users().messages().get(userId="me", id=msg["id"], format="full").execute()
            )

            payload = message["payload"]
            headers = payload["headers"]
            # Look for Subject and Sender Email in the headers
            for d in headers:
                if d["name"] == "Subject":
                    subject = d["value"]
                    # print('subject: ' + str(subject))
                elif d["name"] == "From":
                    sender = d["value"]
                    # print('sender: ' + str(sender))
                elif d["name"] == "Date":
                    emaildate = d["value"]
                    # print('emaildate: ' + str(emaildate))
                elif d["name"] == "Delivered-To":
                    deliveredTo = d["value"]
                    # print('deliveredTo: ' + str(deliveredTo))

            # Get the data and decode it with base 64 decoder.
            emailbody = base64.b64decode(
                payload.get("parts")[0]["body"]["data"].replace("-", "+").replace("_", "/")
            )
            # print('decoded_data: ' + str(emailbody))
            emsg = EmailMessage(
                mfrom=sender, to=deliveredTo, subject=subject, body=emailbody, date=emaildate
            )
            # emsg = EmailMessage(mfrom = mime_msg['From'],to=mime_msg['To'],subject=mime_msg['Subject'],body=message['snippet'],date=mime_msg['Date'])
            messgelist.append(emsg)
            if delete:
                self.DeleteMessage(service, "me", message["id"])

        return messgelist

    def ListMessagesMatchingQuery(self, service, user_id, query="", delete=True):
        """List all Messages of the user's mailbox matching the query.

        Args:
          service: Authorized Gmail API service instance.
          user_id: User's email address. The special value "me"
          can be used to indicate the authenticated user.
          query: String used to filter messages returned.
          Eg.- 'from:user@some_domain.com' for Messages from a particular sender.

        Returns:
          List of Messages that match the criteria of the query. Note that the
          returned list contains Message IDs, you must use get with the
          appropriate ID to get the details of a Message.
        """
        try:
            response = service.users().messages().list(userId=user_id, q=query).execute()
            messages = []
            if "messages" in response:
                messages.extend(response["messages"])

            while "nextPageToken" in response:
                page_token = response["nextPageToken"]
                response = (
                    service.users()
                    .messages()
                    .list(userId=user_id, q=query, pageToken=page_token)
                    .execute()
                )
                messages.extend(response["messages"])

            return messages
        except errors.HttpError as error:
            print("An error occurred: " + str(error))

    def DeleteMessage(self, service, user_id, msg_id):
        """Delete a Message.

        Args:
          service: Authorized Gmail API service instance.
          user_id: User's email address. The special value "me"
          can be used to indicate the authenticated user.
          msg_id: ID of Message to delete.
        """
        try:
            service.users().messages().delete(userId=user_id, id=msg_id).execute()
            print("Message with id:  deleted successfully." + str(msg_id))
        except errors.HttpError as error:
            print("An error occurred: " + str(error))
