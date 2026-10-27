import datetime
from astral.sun import sun
from astral.moon import moonrise
from astral.moon import moonset
from astral import LocationInfo
from geopy.geocoders import Nominatim


class addres:
    def __init__(
        self, streat, country="", number=0, city="", state="", timezone="", Name="My pi homr contro"
    ):
        self.streat = streat
        self.number = number
        self.city = city
        self.locationInfo = None
        if timezone == "":
            self.timezone = country
        else:
            self.timezone = timezone
        if country == "" and state == "":
            raise Exception("missig parameters - you must speficy state or country")
        if country == "":
            self.country = state
        elif state == "":
            self.country = country
        else:
            self.country = state + ", " + country

        if self.city != "":
            if self.streat != "":
                self.fulladderss = (
                    self.streat + ", " + str(self.number) + ", " + self.city + ", " + self.country
                )
            else:
                self.fulladderss = self.city + ", " + self.country
        else:
            self.fulladderss = self.country
        # get cordinate by address
        geolocator = Nominatim(user_agent=Name)
        self.location = geolocator.geocode(self.fulladderss, timeout=None)

    def GetLocation(self):
        return self.location

    def Observer(self):
        if self.locationInfo is None:
            self.locationInfo = LocationInfo(
                latitude=self.location.latitude, longitude=self.location.longitude
            )
        return self.locationInfo.observer


### utility functions #####
def GetAstro(astrotype, date, observer, offset=0):
    # m =  moon(observer,date)
    s = sun(observer, date)
    if astrotype == "moonrise":
        val = moonrise(observer, date) + (datetime.datetime.now() - datetime.datetime.utcnow())
    elif astrotype == "moonset":
        val = moonset(observer, date) + (datetime.datetime.now() - datetime.datetime.utcnow())
    elif astrotype == "dusk":
        val = s["dusk"] + (datetime.datetime.now() - datetime.datetime.utcnow())
    elif astrotype == "dawn":
        val = s["dawn"] + (datetime.datetime.now() - datetime.datetime.utcnow())
    elif astrotype == "sunset":
        val = s["sunset"] + (datetime.datetime.now() - datetime.datetime.utcnow())
    elif astrotype == "sunrise":
        val = s["sunrise"] + (datetime.datetime.now() - datetime.datetime.utcnow())
    elif astrotype == "twilight":
        val = s["twilight"] + (datetime.datetime.now() - datetime.datetime.utcnow())
    elif astrotype == "midnight":
        val = s["midnight"] + (datetime.datetime.now() - datetime.datetime.utcnow())
    elif astrotype == "noon":
        val = s["noon"] + (datetime.datetime.now() - datetime.datetime.utcnow())
    else:
        return None

    return val + datetime.timedelta(minutes=offset)


###### example  #########
##addr = addres(streat = 'hazamir',country='israel',city='ramat gan',timezone='jerusalem')
##dt = datetime.date.today()
##dawn = GetAstro('dawn',dt,addr.Observer())
##################################
