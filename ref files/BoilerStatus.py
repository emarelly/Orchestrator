import time
import datetime
import sys
import json


class BoilerStatus:
    def __init__(self):
        self._mStatus = "off"
        self._mHeaterOnOff = "off"
        self._mDate = datetime.datetime.now()
        self._mMinVal = 0.0
        self._mTargetVal = 0.0
        self._mTimeToStartMin = 0
        self._mCurrentTemp = [-1, -1, -1]
        self._mCurrentShowers = 0.0
        self._mCurrentShowersnew = 0.0
        self._mActiveYesNo = "no"

    def customDict(self):
        dup = self.__dict__.copy()
        # configure dup to contain fields that you want to send
        dup["_mDate"] = self._mDate.isoformat()  # datetime object
        return dup

    def toJSON(self):
        return json.dumps(self, default=lambda o: o.customDict(), sort_keys=True, indent=4).replace(
            "_m", ""
        )

    def get_Status(self):
        return self._mStatus

    def get_HeaterOnOff(self):
        return self._mHeaterOnOff

    def get_Date(self):
        return self._mDate

    def get_MinVal(self):
        return self._mMinVal

    def get_TargetVal(self):
        return self._mTargetVal

    def get_TimeToStartMin(self):
        return self._mTimeToStartMin

    def get_CurrentTemp(self):
        return self._mCurrentTemp

    def get_CurrentShowers(self):
        return self._mCurrentShowers

    def get_CurrentShowersnew(self):
        return self._mCurrentShowersnew

    def get_ActiveYesNo(self):
        return self._mActiveYesNo

    def set_Status(self, a):
        self._mStatus = a

    def set_HeaterOnOff(self, a):
        self._mHeaterOnOff = a

    def set_Date(self, a):
        self._mDate = a

    def set_MinVal(self, a):
        self._mMinVal = a

    def set_TargetVal(self, a):
        self._mTargetVal = a

    def set_TimeToStartMin(self, a):
        self._mTimeToStartMin = a

    def set_CurrentTemp(self, a):
        self._mCurrentTemp = a

    def set_CurrentShowers(self, a):
        self._mCurrentShowers = a

    def set_CurrentShowersnew(self, a):
        self._mCurrentShowersnew = a

    def set_ActiveYesNo(self, a):
        self._mActiveYesNo = a

    Status = property(get_Status, set_Status)
    HeaterOnOff = property(get_HeaterOnOff, set_HeaterOnOff)
    Date = property(get_Date, set_Date)
    MinVal = property(get_MinVal, set_MinVal)
    TargetVal = property(get_TargetVal, set_TargetVal)
    TimeToStartMin = property(get_TimeToStartMin, set_TimeToStartMin)
    CurrentTemp = property(get_CurrentTemp, set_CurrentTemp)
    CurrentShowers = property(get_CurrentShowers, set_CurrentShowers)
    CurrentShowersnew = property(get_CurrentShowersnew, set_CurrentShowersnew)
    ActiveYesNo = property(get_ActiveYesNo, set_ActiveYesNo)
