import pvnet
from pvnet.models.base_model import BaseModel
from pvnet.optimizers import AbstractOptimizer
import pvlib
import pandas as pd
from datetime import timedelta
import torch

class Model(BaseModel):
    name = "smart_persistence"

    def __init__(
        self,
        forecast_minutes: int = 12,
        history_minutes: int = 6,
        latitude: float = 52.0,
        longitude: float = 0.1,
        tz: str = "Etc/UTC",
        altitude: float = 0.0,
        optimizer: AbstractOptimizer = pvnet.optimizers.Adam(),
    ):
        super().__init__(history_minutes, forecast_minutes, optimizer)
        self.latitude = latitude
        self.longitude = longitude
        self.tz = tz
        self.altitude = altitude
        self.location = pvlib.location.Location(latitude, longitude, tz, altitude)
        self.save_hyperparameters()

