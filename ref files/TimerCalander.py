import json
import os


class TimerTask:
    dayOfWeek = 0
    hour = "00:00"
    state = 0
    relaynum = 0

    def __init__(self, dayOfWeek=0, hour="00:00", state=0, relaynum=0, json=""):
        if json != "":
            # self.dayOfWeek = 0
            parts = json.replace("{", "").replace("}", "").split(",")
            for part in parts:
                items = part.replace('"', "").split(":")
                if items[0] == "DayOfWeek":
                    self.dayOfWeek = int(items[1])
                elif items[0] == "Hour":
                    self.hour = items[1] + ":" + items[2]
                    if len(items) > 3:
                        self.hour = self.hour + ":" + items[3]
                elif items[0] == "State":
                    self.state = items[1]
                elif items[0] == "RelayNum":
                    self.relaynum = items[1]
        else:
            self.dayOfWeek = dayOfWeek
            self.hour = hour
            self.state = state
            self.relaynum = relaynum

    def Hash(self):
        return self.state * self.relaynum * self.dayOfWeek

    def concat(self):
        return str(self.dayOfWeek) + self.hour

    def tojson(self):
        # retva = "{" + chr(34) + "DayOfWeek" + chr(34) + ":" + str(self.dayOfWeek) + "," + chr(34) + "Hour" + chr(34) + ":"  + chr(34) + str(self.hour) + chr(34) + "," + chr(34) + "State" + chr(34) + ":" + str(self.state) + "," + chr(34) + "RelayNum" + chr(34) + ":" + str(self.relaynum) + "},"
        retva = (
            '{"DayOfWeek":'
            + str(self.dayOfWeek)
            + ',"Hour":"'
            + str(self.hour)
            + '","State":'
            + str(self.state)
            + ',"RelayNum":'
            + str(self.relaynum)
            + "},"
        )
        return retva

    def toxml(self, name=""):
        retva = (
            '<event Name="'
            + str(name)
            + '">\n<Day>'
            + str(self.dayOfWeek)
            + "</Day>\n<time>"
            + str(self.hour)
            + "</time>\n<RelayNumVal>"
            + str(self.relaynum)
            + "</RelayNumVal>\n<StateVal>"
            + str(self.state)
            + "</StateVal>\n</event>"
        )
        return retva

    def tostring(self):
        retva = (
            "DayOfWeek: "
            + str(self.dayOfWeek)
            + " Start Time: "
            + str(self.hour)
            + " State: "
            + str(self.state)
            + " RelayNum: "
            + str(self.relaynum)
        )
        return retva

    def __eq__(self, other_task):
        if other_task == None:
            return False
        if self.dayOfWeek != other_task.dayOfWeek:
            return False
        if self.hour != other_task.hour:
            return False
        if self.relaynum != other_task.relaynum:
            return False
        if self.state != other_task.state:
            return False
        return True

    def __ne__(self, other_task):
        if other_task == None:
            return True
        if self.dayOfWeek != other_task.dayOfWeek:
            return True
        if self.hour != other_task.hour:
            return True
        if self.relaynum != other_task.relaynum:
            return True
        if self.state != other_task.state:
            return True
        return False


def cmp_to_key(mycmp):
    "Convert a cmp= function into a key= function"

    class K:
        def __init__(self, obj, *args):
            self.obj = obj

        def __lt__(self, other):
            return mycmp(self.obj, other.obj) < 0

        def __gt__(self, other):
            return mycmp(self.obj, other.obj) > 0

        def __eq__(self, other):
            return mycmp(self.obj, other.obj) == 0

        def __le__(self, other):
            return mycmp(self.obj, other.obj) <= 0

        def __ge__(self, other):
            return mycmp(self.obj, other.obj) >= 0

        def __ne__(self, other):
            return mycmp(self.obj, other.obj) != 0

    return K


def numeric_compare(x, y):
    X = x.concat()
    Y = y.concat()
    if X == Y:
        return 0
    if X >= Y:
        return 1
    else:
        return -1


class TimerCalander(object):
    # """description of class"""
    timerTasks = []

    def __init__(self, tasks=[], json=""):
        if json != "":
            tsk = []
            jtasks = json.replace("[", "").replace("]", "").split("},{")
            for jtask in jtasks:
                tsk.append(TimerTask(json=jtask))
            self.timerTasks = tsk
        else:
            self.timerTasks = tasks
        self.RemoveDuplicate()

    def len(self):
        return len(self.timerTasks)

    def tojson(self):
        retva = "["
        for task in self.timerTasks:
            retva = retva + task.tojson()
        return retva[0 : len(retva) - 1] + "]"

    def toxml(self, filename=""):
        retval = "<data>\n"
        i = 0
        for task in self.timerTasks:
            retval = retval + task.toxml(name=i)
            i = i + 1
        retval = retval + "</data>"
        if filename != "":
            with open(filename, "w") as fp:
                fp.write(retval)
        return retval

    def load(self, filename="", json=""):
        if len(filename) > 0:
            if os.path.exists(filename):
                with open(filename, "r") as fp:
                    jsons = fp.read()
                    tsk = []
                    jtasks = jsons.replace("[", "").replace("]", "").split("},{")
                    for jtask in jtasks:
                        tsk.append(TimerTask(json=jtask))
                    self.timerTasks = tsk
                    self.RemoveDuplicate()
        elif len(json) > 0:
            tsk = []
            jtasks = json.replace("[", "").replace("]", "").split("},{")
            for jtask in jtasks:
                tsk.append(TimerTask(json=jtask))
            self.timerTasks = tsk
            self.RemoveDuplicate()

    def save(self, filename="s"):
        if len(filename) > 0:
            with open(filename, "w") as fp:
                strjson = self.tojson()  # ,fp,default = myconverter)
                fp.write(strjson)

    def RemoveDuplicate(self):
        # tmpTasks = sorted (self.timerTasks, cmp = numeric_compare)
        tmpTasks = sorted(self.timerTasks, key=cmp_to_key(numeric_compare))
        out_list = []
        for val in tmpTasks:
            # if not val in added:
            if len(out_list) == 0 or val != out_list[len(out_list) - 1]:
                out_list.append(val)
        self.timerTasks = out_list

    # def ordered_set(in_list):

    def tasks(self, copy=False):
        # self.RemoveDuplicate()
        if copy:
            out_list = []
            for val in self.timerTasks:
                out_list.append(
                    TimerTask(
                        dayOfWeek=val.dayOfWeek,
                        hour=val.hour,
                        state=val.state,
                        relaynum=val.relaynum,
                    )
                )
            return out_list
        else:
            return self.timerTasks

    # operator overloading
    def __eq__(self, other_Cal):
        i = 0
        if other_Cal == None:
            return False
        for task in other_Cal.timerTasks:
            if task != self.timerTasks[i]:
                return False
            i = i + 1
        return True

    def __ne__(self, other_Cal):
        i = 0
        if other_Cal == None:
            return False
        for task in other_Cal.timerTasks:
            if task != self.timerTasks[i]:
                return True
            i = i + 1
        return False

    def diff(self, other_cal):
        addedtasks = []
        removedtasks = []
        found = False
        for ot in other_cal.timerTasks:
            for t in self.timerTasks:
                if ot == t:
                    found = True
                    break
            if found == False:  # if not found
                addedtasks.append(
                    TimerTask(
                        dayOfWeek=ot.dayOfWeek, hour=ot.hour, state=ot.state, relaynum=ot.relaynum
                    )
                )
            found = False
        for t in self.timerTasks:
            for ot in other_cal.timerTasks:
                if ot == t:
                    found = True
                    break
            if found == False:  # if not found
                removedtasks.append(
                    TimerTask(
                        dayOfWeek=t.dayOfWeek, hour=t.hour, state=t.state, relaynum=t.relaynum
                    )
                )
            found = False
        # print differences
        retval = ""
        newline = ""
        if len(addedtasks) > 0:
            retval = "The following dates were add to the calander:"
            newline = "\r\n"
            for t in addedtasks:
                retval = retval + newline + t.tostring()
        if len(removedtasks) > 0:
            retval = retval + newline + "The following dates were removed the calander:"
            newline = "\r\n"
            for t in removedtasks:
                retval = retval + newline + t.tostring()
        return retval
