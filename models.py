from enum import Enum
from passlib.hash import argon2
from pony.orm import Database, Required, Set

db = Database()

class NeedleType(str, Enum):
    LONG  = "long"
    SHORT = "short"

class GaugeType(db.Entity):
    max_value    = Required(int)
    min_value    = Required(int)
    start_degree = Required(int)
    end_degree   = Required(int)
    needle_type  = Required(str, default=NeedleType.LONG.value)
    calibrations = Set('GaugeCalibration')

class CctvConnection(db.Entity):
    url          = Required(str)
    name         = Required(str)
    user         = Required(str)
    password     = Required(str)
    calibrations = Set('GaugeCalibration')

class GaugeCalibration(db.Entity):
    gauge_type      = Required(GaugeType)
    cctv_connection = Required(CctvConnection)

class User(db.Entity):
    name = Required(str)
    email = Required(str, unique=True)
    password = Required(str)

    def set_password(self, raw_password):
        self.password = argon2.hash(raw_password)

    def verify_password(self, raw_password):
        return argon2.verify(raw_password, self.password)