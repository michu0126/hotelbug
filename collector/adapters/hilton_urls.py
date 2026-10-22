"""Public browser navigation paths observed in the supplied Hilton HARs."""

import re
from urllib.parse import urlencode

from models import Stay

ROOT = "https://www.hilton.com"
ROOMS_PATH = "/zh-hans/book/reservation/rooms/"
RATES_PATH = "/zh-hans/book/reservation/rates/"


def rooms_url(hotel_id: str, stay: Stay):
    if not re.fullmatch(r"[A-Z0-9]{5,10}", hotel_id) or stay.rooms != 1:
        raise ValueError("Hilton requires an explicit hotel code and one room")
    return (
        ROOT
        + ROOMS_PATH
        + "?"
        + urlencode(
            {
                "ctyhocn": hotel_id,
                "arrivalDate": stay.check_in.isoformat(),
                "departureDate": stay.check_out.isoformat(),
                "room1NumAdults": stay.adults,
            }
        )
    )
