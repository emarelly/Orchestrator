from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
import threading
import time
import json
import sys
import os.path
import datetime
from itertools import islice
from collections import deque
import BoilerStatus
import BoilerCalander
import TimerCalander
import controlrelay
import Config

# utils functions
# read history file and get it in JS rows format


def gethistoricaldata(filename):
    rows = ""
    if os.path.isfile(filename):
        fp = open(filename, "r")
        history = fp.readlines()
        fp.close()
        # file is a 1 min list and we want 10 min sample for lst 24 hours so 144 samples out of 1440 lines
        startpoint = len(history) - 1441
        if startpoint < 0:
            startpoint = 0
        for i in range(startpoint, len(history), 10):
            try:
                line = json.loads(history[i].replace(";", "").replace("'", ""))
                temp = float(int(line["Capacity"][0])) / 10
                newtemp = float(int(line["Capacity"][1])) / 10
                if history[i].find("target") >= 0:
                    target = line["target"]
                    if len(line["target"]) < 3:
                        line["target"].append(-1)
                else:
                    target = [2.2, 3.3, -1]
                dateparts = line["date"].split(":")
                dateparts[0] = dateparts[0].split("/")
                item = (
                    "["
                    + dateparts[0][0]
                    + ","
                    + dateparts[0][1]
                    + ","
                    + dateparts[0][2].strip()
                    + ","
                    + dateparts[1].strip()
                    + ","
                    + dateparts[2]
                    + ","
                    + dateparts[3]
                    + ","
                    + str(temp)
                    + ","
                    + str(target[0])
                    + ","
                    + str(target[1])
                    + ","
                    + str(target[2])
                    + ","
                    + str(newtemp)
                    + "],"
                )
                rows = rows + item
            except:
                print("ignoring line " + history[i])
    return (rows + "]").replace("],]", "]")


# file head implmentatioin
def head(filename, NOFlines=150):
    if os.path.isfile(filename):
        with open("datafile") as myfile:
            head = list(islice(myfile, NOFlines))
        return str(head)
    else:
        return "file not found"


# file tail implimentation
def tail(filename, NOFlines=150):
    if os.path.isfile(filename):
        with open(filename) as f:
            return str(deque(f, NOFlines))
    else:
        return "file not found"


# generate manual calander file
def genmanualcal(hours=3, temp=4, filename="manual.json"):
    CurrentDate = datetime.datetime.now()
    currentTime = CurrentDate.time()
    CurentDayOfWeek = CurrentDate.weekday() + 2
    if CurentDayOfWeek == 8:
        CurentDayOfWeek = 1
    hr = currentTime.hour + hours
    if currentTime.minute > 29:
        hr += 1
    if hr >= 24:
        hr = hr - 24
        CurentDayOfWeek += 1
    tmphr = ("0" + str(hr))[-2:] + ":00:00"
    manual = BoilerCalander.BoilerCalander(
        tasks=[
            BoilerCalander.BoilerTask(dayOfWeek=CurentDayOfWeek, hour=tmphr, target=temp, min=temp)
        ]
    )
    manual.save(filename=filename)


## generate manual swithc jason
def genmanualcal4relay(on=True, duration=60, relay=2, filename="2Timemanual.json"):
    CurrentDate = datetime.datetime.now()
    CurrentDate = CurrentDate + datetime.timedelta(seconds=90)
    if CurrentDate.weekday() == 6:
        day = 1
    else:
        day = CurrentDate.weekday() + 2
    time = (
        "{:02d}".format(CurrentDate.hour)
        + ":"
        + "{:02d}".format(CurrentDate.minute)
        + ":"
        + "{:02d}".format(CurrentDate.second)
    )
    ttask = TimerCalander.TimerTask(dayOfWeek=day, hour=time, relaynum=relay, state=1 if on else 0)
    Timertasks = [ttask]
    if (
        duration > 0
    ):  # 0 or negative duration = on or off without timer, positive duration = switch between on and off with timer
        CurrentDate = CurrentDate + datetime.timedelta(seconds=int(duration))
        if CurrentDate.weekday() == 6:
            day = 1
        else:
            day = CurrentDate.weekday() + 2
        time = (
            "{:02d}".format(CurrentDate.hour)
            + ":"
            + "{:02d}".format(CurrentDate.minute)
            + ":"
            + "{:02d}".format(CurrentDate.second)
        )
        ttask = TimerCalander.TimerTask(
            dayOfWeek=day, hour=time, relaynum=relay, state=0 if on else 1
        )
        Timertasks.append(ttask)
    manual = TimerCalander.TimerCalander(tasks=Timertasks)
    manual.save(filename=filename)


# http server wrapper class with shared status class
class http_server:
    def __init__(self, ip, port, status):
        def handler(*args):
            BoilerRestHTTPRequestHandler(status, *args)

        self.server = HTTPServer((ip, port), handler)

    def serve_forever(self):
        self.server.serve_forever()


# request handler
class BoilerRestHTTPRequestHandler(BaseHTTPRequestHandler):
    def __init__(self, status, *args):
        # print ('BoilerRestHTTPRequestHandler called')
        self.status = status
        print("Http Server Started")
        BaseHTTPRequestHandler.__init__(self, *args)

    def do_GET(self):
        query = urlparse(self.path).query
        folder = urlparse(self.path).path.replace("/", "").lower()
        if len(query) > 0:
            query_components = dict(qc.split("=") for qc in query.split("&"))
        else:
            query_components = None
        if folder == "status":
            out = self.status.toJSON()
        elif folder == "history":
            out = gethistoricaldata("temp.txt")
        elif folder == "nicestatus":
            # out = '<!DOCTYPE html> \n <html> \n <body> \n  <h1>Heading 1</h1> \n <h2>Heading 2</h2> \n <h3>Heading 3</h3> \n <h4>Heading 4</h4> \n</body>\n </html>'
            f = open("nice.html")
            out = f.read()
            f.close()
        elif folder == "log":
            out = "log file is: \r\n" + tail("BoilerManager.log", 150)
        elif folder == "relay":
            if query_components is not None and query.find("relay") >= 0:
                relay = query_components["relay"]
                if (controlrelay.ReadRelay(int(query_components["relay"]))) == 0:
                    out = (
                        "Relay "
                        + str(query_components["relay"])
                        + "is off \r\n"
                        + tail(Config.AuditFileName + query_components["relay"] + ".log", 20)
                    )
                else:
                    out = (
                        "Relay "
                        + str(query_components["relay"])
                        + "is on \r\n"
                        + tail(Config.AuditFileName + query_components["relay"] + ".log", 20)
                    )
            else:
                out = "missing parameter"
        else:
            out = "Hello, world! folder " + folder
        self.send_response(200, "OK")
        self.send_header("Content-type", "text/html")
        self.end_headers()
        self.wfile.write(out.encode(encoding="utf-8", errors="strict"))

    def do_POST(self):
        query = urlparse(self.path).query
        folder = urlparse(self.path).path.replace("/", "").lower()
        if len(query) > 0:
            query_components = dict(qc.split("=") for qc in query.split("&"))
        if folder == "updatecalander":
            content_length = int(self.headers["Content-Length"])  # <--- Gets the size of data
            lines = self.rfile.read(content_length).decode("utf-8").splitlines()
            with open(Config.CalendarLocalPath, "w") as myfile:
                myfile.writelines(lines[3])
        elif folder == "manualon":
            genmanualcal(hours=3, temp=4)
        elif folder == "auto":
            if os.path.exists(Config.ManualLocalPath):
                os.remove(Config.ManualLocalPath)
        elif folder == "manualoff":
            genmanualcal(hours=1, temp=0)
        elif folder == "updaterelaycalander":
            content_length = int(self.headers["Content-Length"])  # <--- Gets the size of data
            lines = self.rfile.read(content_length).decode("utf-8").splitlines()
            with open(Config.TimerCalendarLocalPath, "w") as myfile:
                myfile.writelines(lines[3])
        elif folder == "relayon":
            content_length = int(self.headers["Content-Length"])  # <--- Gets the size of data
            lines = self.rfile.read(content_length).decode("utf-8").splitlines()
            data = json.loads(lines[0])
            genmanualcal4relay(
                on=True,
                duration=data["duration"],
                relay=data["swith"],
                filename=str(data["swith"]) + Config.TimerManualLocalPath,
            )
        elif folder == "relayoff":
            content_length = int(self.headers["Content-Length"])  # <--- Gets the size of data
            lines = self.rfile.read(content_length).decode("utf-8").splitlines()
            data = json.loads(lines[0])
            genmanualcal4relay(
                on=False,
                duration=data["duration"],
                relay=data["swith"],
                filename=str(data["swith"]) + Config.TimerManualLocalPath,
            )
            # genmanualcal4relay(duration=0,relay=data['swith'],filename=str(data['swith']) + Config.TimerManualLocalPath)
        self.send_response(200, "OK")
        self.send_header("Content-type", "text/html")
        self.end_headers()
        self.wfile.write("POST request for {}".format(self.path).encode("utf-8"))


def runserver(status):
    httpd = http_server("0.0.0.0", 8000, status)
    httpd.serve_forever()
    print("http server terminated ....")
