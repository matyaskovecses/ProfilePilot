"""Identity autofill: detect the fields of any form and fill them from user-entered values.

:func:`detect_fields` scans the main frame and every visible child frame - including cross-origin
card iframes (Stripe, Braintree) - with one read-only JavaScript function per frame (nothing is
written to the DOM: no marker attributes) and classifies each control in Python from, in order of
strength:

1. the ``autocomplete`` tokens (``section-*`` / ``shipping`` / ``billing`` prefixes allowed);
2. the input ``type`` (``email``, ``password``; ``date``/``month`` with a birth or card signal);
3. regular expressions over ``name``, ``id``, ``placeholder``, ``aria-label``, the associated
   ``<label>``, ``aria-labelledby``, ``title`` and nearby preceding text / ``<legend>`` (English,
   German, French and Spanish);
4. ``inputmode`` / ``maxlength`` / ``pattern`` hints (formats, split fields) and the shape of a
   select's options (days, months, years).

:func:`autofill` fills what was detected: text controls through
:func:`~profilepilot.automation.typing.enter_text` (``paste`` by default), ``<select>`` elements by
fuzzy option matching (countries by name / ISO-2 / ISO-3 / numeric code, US states and Canadian
provinces by name or code, months as ``01`` / ``1`` / ``Jan`` / ``January`` in four languages,
2- or 4-digit years, days, gender), date inputs with ``fill(YYYY-MM-DD)``, radio groups by
clicking the matching option, and split fields (phone 3-3-4, SSN 3-2-4, card 4x4, DOB MM/DD/YYYY)
by distributing the digits over the parts by ``maxlength``.

Values are never generated: only what is in ``values`` is filled. :class:`AutofillReport` holds
kinds, field descriptors and methods only - never values (descriptors are additionally scrubbed of
every value). Sensitive keys (SSN, card, password) are filled only when the caller put them in
``values``; the server does that only after ``IdentityStore.check_sensitive_origin`` accepted the
top-level page URL.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Iterable, Literal

from ..identity import SENSITIVE_FIELDS, derived_values
from .typing import NotTypeableError, TextEntryError, TypeMethod, enter_text

if TYPE_CHECKING:
    from .driver import ElementHandle, Frame, Locator, Page

log = logging.getLogger("profilepilot.autofill")

Control = Literal["text", "select", "date", "radio", "checkbox", "contenteditable"]

FIELD_PAUSE = (0.15, 0.6)
"""Human pause between fields (seconds) for the ``human`` and ``paste`` methods."""
ACTION_TIMEOUT_MS = 10_000
OPTION_WAIT_SECONDS = 1.5
"""How long a deferred select waits for its options to load (e.g. states after the country)."""
MAX_CONTROLS_PER_FRAME = 400

# ---------------------------------------------------------------------- kinds

KIND_SOURCES: dict[str, frozenset[str]] = {k: frozenset(v) for k, v in {
    "first_name": {"first_name"}, "middle_name": {"middle_name"}, "last_name": {"last_name"},
    "full_name": {"full_name", "first_name", "middle_name", "last_name"},
    "email": {"email"}, "username": {"username"}, "password": {"password"},
    "phone": {"phone"}, "phone_national": {"phone"}, "phone_country_code": {"phone"},
    "phone_area": {"phone"}, "phone_local": {"phone"}, "phone_prefix": {"phone"}, "phone_suffix": {"phone"},
    "company": {"company"}, "street": {"street"}, "street_name": {"street"}, "house_number": {"street"},
    "address_line2": {"address_line2"}, "city": {"city"},
    "state": {"state"}, "postal_code": {"postal_code"}, "country": {"country", "country_code"},
    "birth_date": {"birth_date"}, "birth_day": {"birth_date"}, "birth_month": {"birth_date"},
    "birth_year": {"birth_date"}, "gender": {"gender"},
    "card_name": {"card_name", "first_name", "last_name"}, "card_number": {"card_number"},
    "card_exp": {"card_exp_month", "card_exp_year"}, "card_exp_month": {"card_exp_month"},
    "card_exp_year": {"card_exp_year"}, "card_cvv": {"card_cvv"}, "card_type": {"card_number"},
    "ssn": {"ssn"},
}.items()}
"""Detected field kind -> identity keys its value comes from (used for ``only`` and sensitivity)."""
NOT_SECRET_KINDS = frozenset({"card_type", "card_name"})
"""Derived from sensitive data but not secret themselves (a card brand, the holder's name)."""

# ---------------------------------------------------------------------- embedded tables

_ISO3166 = """\
AF AFG 004 Afghanistan
AX ALA 248 Aland Islands|Åland
AL ALB 008 Albania|Albanien|Albanie
DZ DZA 012 Algeria|Algerien|Algérie|Argelia
AS ASM 016 American Samoa
AD AND 020 Andorra|Andorre
AO AGO 024 Angola
AI AIA 660 Anguilla
AQ ATA 010 Antarctica
AG ATG 028 Antigua and Barbuda|Antigua & Barbuda
AR ARG 032 Argentina|Argentinien|Argentine
AM ARM 051 Armenia|Armenien|Arménie
AW ABW 533 Aruba
AU AUS 036 Australia|Australien|Australie
AT AUT 040 Austria|Österreich|Autriche
AZ AZE 031 Azerbaijan|Aserbaidschan
BS BHS 044 Bahamas|The Bahamas
BH BHR 048 Bahrain
BD BGD 050 Bangladesh|Bangladesch
BB BRB 052 Barbados
BY BLR 112 Belarus|Weißrussland|Biélorussie|Bielorrusia
BE BEL 056 Belgium|Belgien|Belgique|Bélgica
BZ BLZ 084 Belize
BJ BEN 204 Benin|Bénin
BM BMU 060 Bermuda|Bermudes
BT BTN 064 Bhutan
BO BOL 068 Bolivia|Bolivia, Plurinational State of|Bolivien|Bolivie
BQ BES 535 Bonaire, Sint Eustatius and Saba|Caribbean Netherlands
BA BIH 070 Bosnia and Herzegovina|Bosnia & Herzegovina|Bosnien und Herzegowina
BW BWA 072 Botswana
BV BVT 074 Bouvet Island
BR BRA 076 Brazil|Brasil|Brasilien|Brésil
IO IOT 086 British Indian Ocean Territory
BN BRN 096 Brunei Darussalam|Brunei
BG BGR 100 Bulgaria|Bulgarien|Bulgarie
BF BFA 854 Burkina Faso
BI BDI 108 Burundi
CV CPV 132 Cabo Verde|Cape Verde|Kap Verde
KH KHM 116 Cambodia|Kambodscha|Cambodge|Camboya
CM CMR 120 Cameroon|Kamerun|Cameroun|Camerún
CA CAN 124 Canada|Kanada|Canadá
KY CYM 136 Cayman Islands
CF CAF 140 Central African Republic
TD TCD 148 Chad|Tschad|Tchad
CL CHL 152 Chile|Chili
CN CHN 156 China|People's Republic of China|Chine|VR China
CX CXR 162 Christmas Island
CC CCK 166 Cocos (Keeling) Islands
CO COL 170 Colombia|Kolumbien|Colombie
KM COM 174 Comoros|Komoren|Comores
CG COG 178 Congo|Republic of the Congo|Congo-Brazzaville
CD COD 180 Congo, Democratic Republic of the|Democratic Republic of the Congo|DR Congo|Congo-Kinshasa
CK COK 184 Cook Islands
CR CRI 188 Costa Rica
CI CIV 384 Cote d'Ivoire|Ivory Coast|Elfenbeinküste
HR HRV 191 Croatia|Kroatien|Croatie|Croacia|Hrvatska
CU CUB 192 Cuba|Kuba
CW CUW 531 Curacao|Curaçao
CY CYP 196 Cyprus|Zypern|Chypre|Chipre
CZ CZE 203 Czechia|Czech Republic|Tschechien|Tschechische Republik|République tchèque|Chequia|República Checa
DK DNK 208 Denmark|Dänemark|Danemark|Dinamarca
DJ DJI 262 Djibouti|Dschibuti
DM DMA 212 Dominica
DO DOM 214 Dominican Republic|Dominikanische Republik|República Dominicana
EC ECU 218 Ecuador|Équateur
EG EGY 818 Egypt|Ägypten|Égypte|Egipto
SV SLV 222 El Salvador
GQ GNQ 226 Equatorial Guinea
ER ERI 232 Eritrea|Érythrée
EE EST 233 Estonia|Estland|Estonie
SZ SWZ 748 Eswatini|Swaziland
ET ETH 231 Ethiopia|Äthiopien|Éthiopie|Etiopía
FK FLK 238 Falkland Islands (Malvinas)|Falkland Islands
FO FRO 234 Faroe Islands|Färöer
FJ FJI 242 Fiji|Fidschi
FI FIN 246 Finland|Finnland|Finlande|Finlandia
FR FRA 250 France|Frankreich|Francia
GF GUF 254 French Guiana|Französisch-Guayana|Guyane
PF PYF 258 French Polynesia|Französisch-Polynesien|Polynésie française
TF ATF 260 French Southern Territories
GA GAB 266 Gabon|Gabun|Gabón
GM GMB 270 Gambia|The Gambia|Gambie
GE GEO 268 Georgia|Georgien|Géorgie
DE DEU 276 Germany|Deutschland|Allemagne|Alemania|Federal Republic of Germany|Bundesrepublik Deutschland
GH GHA 288 Ghana
GI GIB 292 Gibraltar
GR GRC 300 Greece|Griechenland|Grèce|Grecia|Hellas
GL GRL 304 Greenland|Grönland|Groenland
GD GRD 308 Grenada
GP GLP 312 Guadeloupe
GU GUM 316 Guam
GT GTM 320 Guatemala
GG GGY 831 Guernsey
GN GIN 324 Guinea|Guinée
GW GNB 624 Guinea-Bissau
GY GUY 328 Guyana
HT HTI 332 Haiti|Haïti|Haití
HM HMD 334 Heard Island and McDonald Islands
VA VAT 336 Holy See|Vatican City|Vatican|Vatikanstadt
HN HND 340 Honduras
HK HKG 344 Hong Kong|Hongkong
HU HUN 348 Hungary|Ungarn|Hongrie|Hungría|Magyarország
IS ISL 352 Iceland|Island|Islande|Islandia
IN IND 356 India|Indien|Inde
ID IDN 360 Indonesia|Indonesien|Indonésie
IR IRN 364 Iran|Iran, Islamic Republic of
IQ IRQ 368 Iraq|Irak
IE IRL 372 Ireland|Irland|Irlande|Irlanda
IM IMN 833 Isle of Man
IL ISR 376 Israel|Israël
IT ITA 380 Italy|Italien|Italie|Italia
JM JAM 388 Jamaica|Jamaika|Jamaïque
JP JPN 392 Japan|Japon|Japón
JE JEY 832 Jersey
JO JOR 400 Jordan|Jordanien|Jordanie|Jordania
KZ KAZ 398 Kazakhstan|Kasachstan|Kazajistán
KE KEN 404 Kenya|Kenia
KI KIR 296 Kiribati
KP PRK 408 Korea, Democratic People's Republic of|North Korea|Nordkorea
KR KOR 410 Korea, Republic of|South Korea|Korea|Südkorea|Corée du Sud|Corea del Sur
KW KWT 414 Kuwait|Koweït
KG KGZ 417 Kyrgyzstan|Kirgisistan
LA LAO 418 Lao People's Democratic Republic|Laos
LV LVA 428 Latvia|Lettland|Lettonie|Letonia
LB LBN 422 Lebanon|Libanon|Liban|Líbano
LS LSO 426 Lesotho
LR LBR 430 Liberia
LY LBY 434 Libya|Libyen|Libye|Libia
LI LIE 438 Liechtenstein
LT LTU 440 Lithuania|Litauen|Lituanie|Lituania
LU LUX 442 Luxembourg|Luxemburg|Luxemburgo
MO MAC 446 Macao|Macau
MG MDG 450 Madagascar|Madagaskar
MW MWI 454 Malawi
MY MYS 458 Malaysia|Malaisie|Malasia
MV MDV 462 Maldives|Malediven
ML MLI 466 Mali
MT MLT 470 Malta|Malte
MH MHL 584 Marshall Islands
MQ MTQ 474 Martinique
MR MRT 478 Mauritania|Mauretanien|Mauritanie
MU MUS 480 Mauritius|Maurice
YT MYT 175 Mayotte
MX MEX 484 Mexico|México|Mexiko|Mexique
FM FSM 583 Micronesia|Micronesia, Federated States of
MD MDA 498 Moldova|Moldova, Republic of|Moldawien|Moldavie
MC MCO 492 Monaco|Mónaco
MN MNG 496 Mongolia|Mongolei|Mongolie
ME MNE 499 Montenegro|Monténégro
MS MSR 500 Montserrat
MA MAR 504 Morocco|Marokko|Maroc|Marruecos
MZ MOZ 508 Mozambique|Mosambik
MM MMR 104 Myanmar|Burma
NA NAM 516 Namibia|Namibie
NR NRU 520 Nauru
NP NPL 524 Nepal|Népal
NL NLD 528 Netherlands|The Netherlands|Holland|Niederlande|Pays-Bas|Países Bajos|Nederland
NC NCL 540 New Caledonia|Neukaledonien|Nouvelle-Calédonie
NZ NZL 554 New Zealand|Neuseeland|Nouvelle-Zélande|Nueva Zelanda
NI NIC 558 Nicaragua
NE NER 562 Niger
NG NGA 566 Nigeria|Nigéria
NU NIU 570 Niue
NF NFK 574 Norfolk Island
MK MKD 807 North Macedonia|Macedonia|Nordmazedonien|Macédoine du Nord
MP MNP 580 Northern Mariana Islands
NO NOR 578 Norway|Norwegen|Norvège|Noruega|Norge
OM OMN 512 Oman
PK PAK 586 Pakistan
PW PLW 585 Palau
PS PSE 275 Palestine, State of|Palestine|Palästina
PA PAN 591 Panama|Panamá
PG PNG 598 Papua New Guinea|Papua-Neuguinea
PY PRY 600 Paraguay
PE PER 604 Peru|Pérou|Perú
PH PHL 608 Philippines|Philippinen|Filipinas
PN PCN 612 Pitcairn
PL POL 616 Poland|Polen|Pologne|Polonia|Polska
PT PRT 620 Portugal
PR PRI 630 Puerto Rico|Porto Rico
QA QAT 634 Qatar|Katar
RE REU 638 Reunion|Réunion
RO ROU 642 Romania|Rumänien|Roumanie|Rumania
RU RUS 643 Russian Federation|Russia|Russland|Russie|Rusia
RW RWA 646 Rwanda|Ruanda
BL BLM 652 Saint Barthelemy|Saint Barthélemy
SH SHN 654 Saint Helena, Ascension and Tristan da Cunha|Saint Helena
KN KNA 659 Saint Kitts and Nevis
LC LCA 662 Saint Lucia
MF MAF 663 Saint Martin (French part)|Saint Martin
PM SPM 666 Saint Pierre and Miquelon
VC VCT 670 Saint Vincent and the Grenadines
WS WSM 882 Samoa
SM SMR 674 San Marino|Saint-Marin
ST STP 678 Sao Tome and Principe
SA SAU 682 Saudi Arabia|Saudi-Arabien|Arabie saoudite|Arabia Saudita|Arabia Saudí
SN SEN 686 Senegal|Sénégal
RS SRB 688 Serbia|Serbien|Serbie
SC SYC 690 Seychelles|Seychellen
SL SLE 694 Sierra Leone
SG SGP 702 Singapore|Singapur|Singapour
SX SXM 534 Sint Maarten (Dutch part)|Sint Maarten
SK SVK 703 Slovakia|Slowakei|Slovaquie|Eslovaquia|Slovak Republic
SI SVN 705 Slovenia|Slowenien|Slovénie|Eslovenia
SB SLB 090 Solomon Islands
SO SOM 706 Somalia|Somalie
ZA ZAF 710 South Africa|Südafrika|Afrique du Sud|Sudáfrica
GS SGS 239 South Georgia and the South Sandwich Islands
SS SSD 728 South Sudan|Südsudan
ES ESP 724 Spain|España|Spanien|Espagne
LK LKA 144 Sri Lanka
SD SDN 729 Sudan|Soudan|Sudán
SR SUR 740 Suriname|Surinam
SJ SJM 744 Svalbard and Jan Mayen
SE SWE 752 Sweden|Schweden|Suède|Suecia|Sverige
CH CHE 756 Switzerland|Schweiz|Suisse|Svizzera|Suiza
SY SYR 760 Syrian Arab Republic|Syria|Syrien|Syrie|Siria
TW TWN 158 Taiwan|Taiwan, Province of China
TJ TJK 762 Tajikistan|Tadschikistan
TZ TZA 834 Tanzania|Tanzania, United Republic of|Tansania
TH THA 764 Thailand|Thaïlande|Tailandia
TL TLS 626 Timor-Leste|East Timor|Osttimor
TG TGO 768 Togo
TK TKL 772 Tokelau
TO TON 776 Tonga
TT TTO 780 Trinidad and Tobago|Trinidad & Tobago
TN TUN 788 Tunisia|Tunesien|Tunisie|Túnez
TR TUR 792 Turkiye|Türkiye|Turkey|Türkei|Turquie|Turquía
TM TKM 795 Turkmenistan
TC TCA 796 Turks and Caicos Islands
TV TUV 798 Tuvalu
UG UGA 800 Uganda|Ouganda
UA UKR 804 Ukraine|Ucrania
AE ARE 784 United Arab Emirates|UAE|Vereinigte Arabische Emirate|Émirats arabes unis|Emiratos Árabes Unidos
GB GBR 826 United Kingdom|UK|U.K.|Great Britain|Britain|England|United Kingdom of Great Britain and Northern Ireland|Großbritannien|Vereinigtes Königreich|Royaume-Uni|Reino Unido
US USA 840 United States|United States of America|USA|U.S.A.|U.S.|US|America|Vereinigte Staaten|Vereinigte Staaten von Amerika|États-Unis|Etats-Unis|Estados Unidos
UM UMI 581 United States Minor Outlying Islands
UY URY 858 Uruguay
UZ UZB 860 Uzbekistan|Usbekistan
VU VUT 548 Vanuatu
VE VEN 862 Venezuela|Venezuela, Bolivarian Republic of
VN VNM 704 Viet Nam|Vietnam
VG VGB 092 Virgin Islands (British)|British Virgin Islands
VI VIR 850 Virgin Islands (U.S.)|US Virgin Islands|U.S. Virgin Islands
WF WLF 876 Wallis and Futuna
EH ESH 732 Western Sahara
YE YEM 887 Yemen|Jemen|Yémen
ZM ZMB 894 Zambia|Sambia|Zambie
ZW ZWE 716 Zimbabwe|Simbabwe
"""

_US_STATES = """\
AL Alabama|AK Alaska|AZ Arizona|AR Arkansas|CA California|CO Colorado|CT Connecticut|DE Delaware|\
FL Florida|GA Georgia|HI Hawaii|ID Idaho|IL Illinois|IN Indiana|IA Iowa|KS Kansas|KY Kentucky|\
LA Louisiana|ME Maine|MD Maryland|MA Massachusetts|MI Michigan|MN Minnesota|MS Mississippi|\
MO Missouri|MT Montana|NE Nebraska|NV Nevada|NH New Hampshire|NJ New Jersey|NM New Mexico|\
NY New York|NC North Carolina|ND North Dakota|OH Ohio|OK Oklahoma|OR Oregon|PA Pennsylvania|\
RI Rhode Island|SC South Carolina|SD South Dakota|TN Tennessee|TX Texas|UT Utah|VT Vermont|\
VA Virginia|WA Washington|WV West Virginia|WI Wisconsin|WY Wyoming|DC District of Columbia|\
PR Puerto Rico|GU Guam|VI U.S. Virgin Islands|AS American Samoa|MP Northern Mariana Islands|\
AA Armed Forces Americas|AE Armed Forces Europe|AP Armed Forces Pacific"""

_CA_PROVINCES = """\
AB Alberta|BC British Columbia|MB Manitoba|NB New Brunswick|NL Newfoundland and Labrador|\
NS Nova Scotia|NT Northwest Territories|NU Nunavut|ON Ontario|PE Prince Edward Island|\
QC Quebec|SK Saskatchewan|YT Yukon"""

_MONTHS = [  # en, en-abbr, de, de-abbr, fr, fr-abbr, es, es-abbr
    ("January", "Jan", "Januar", "Jan", "janvier", "janv", "enero", "ene", "Jänner"),
    ("February", "Feb", "Februar", "Feb", "février", "févr", "febrero", "feb"),
    ("March", "Mar", "März", "Mrz", "mars", "mars", "marzo", "mar", "Mär"),
    ("April", "Apr", "April", "Apr", "avril", "avr", "abril", "abr"),
    ("May", "May", "Mai", "Mai", "mai", "mai", "mayo", "may"),
    ("June", "Jun", "Juni", "Jun", "juin", "juin", "junio", "jun"),
    ("July", "Jul", "Juli", "Jul", "juillet", "juil", "julio", "jul"),
    ("August", "Aug", "August", "Aug", "août", "aout", "agosto", "ago"),
    ("September", "Sep", "September", "Sept", "septembre", "sept", "septiembre", "setiembre"),
    ("October", "Oct", "Oktober", "Okt", "octobre", "oct", "octubre", "oct"),
    ("November", "Nov", "November", "Nov", "novembre", "nov", "noviembre", "nov"),
    ("December", "Dec", "Dezember", "Dez", "décembre", "déc", "diciembre", "dic"),
]

_GENDER_WORDS = {
    "male": ("male", "m", "man", "men", "masculine", "mr", "mister", "herr", "mann", "mannlich", "homme",
             "masculin", "monsieur", "m.", "hombre", "masculino", "senor", "sr", "he him"),
    "female": ("female", "f", "w", "woman", "women", "feminine", "mrs", "ms", "miss", "frau", "weiblich",
               "femme", "feminin", "madame", "mme", "mujer", "femenino", "senora", "sra", "she her"),
    "other": ("other", "divers", "diverse", "d", "x", "non binary", "nonbinary", "non-binary", "autre", "otro",
              "otra", "andere", "anderes"),
}
_CARD_BRANDS = {
    "visa": ("visa", "vi"), "mastercard": ("mastercard", "master card", "mc", "master"),
    "amex": ("amex", "american express", "ax"), "discover": ("discover", "disc", "di"),
}

_PLACEHOLDER_OPTION = re.compile(
    r"^\s*(?:$|-+|—|\.\.\.|select\b|choose|please|pick|bitte|w[aä]hlen|ausw[aä]hlen|choisi|s[eé]lection|"
    r"selecci|elige|seleccione|escoja)", re.IGNORECASE)


def _fold(text: str) -> str:
    """Casefold, strip accents (``März`` -> ``marz``, ``Straße`` -> ``strasse``), collapse spaces."""
    text = unicodedata.normalize("NFKD", str(text).casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(text.split())


def _key(text: str) -> str:
    """Alphanumeric-only fold for exact option comparisons (``U.S.A.`` -> ``usa``)."""
    return re.sub(r"[^0-9a-z]", "", _fold(text))


@dataclass(frozen=True)
class Country:
    code2: str
    code3: str
    numeric: str
    names: tuple[str, ...]


def _load_countries() -> tuple[dict[str, Country], dict[str, Country]]:
    by_code: dict[str, Country] = {}
    by_key: dict[str, Country] = {}
    for line in _ISO3166.strip().splitlines():
        code2, code3, numeric, rest = line.split(" ", 3)
        country = Country(code2, code3, numeric, tuple(rest.split("|")))
        by_code[code2] = country
        for name in country.names:
            by_key.setdefault(_key(name), country)
        by_key.setdefault(_key(code3), country)
    return by_code, by_key


COUNTRIES, _COUNTRY_KEYS = _load_countries()


def _load_regions(blob: str) -> dict[str, str]:
    return {item[:2]: item[3:] for item in blob.split("|")}


US_STATES = _load_regions(_US_STATES)
CA_PROVINCES = _load_regions(_CA_PROVINCES)


def find_country(text: str | None) -> Country | None:
    """Country by ISO-2, ISO-3, numeric code, English name or a common local name."""
    if not text:
        return None
    raw = str(text).strip()
    if re.fullmatch(r"[A-Za-z]{2}", raw):
        return COUNTRIES.get(raw.upper())
    if re.fullmatch(r"\d{3}", raw):
        return next((c for c in COUNTRIES.values() if c.numeric == raw), None)
    return _COUNTRY_KEYS.get(_key(raw))


def find_region(text: str | None, country_code: str | None = None) -> tuple[str, str] | None:
    """``(code, name)`` of a US state or Canadian province given by code or name."""
    if not text:
        return None
    tables = [CA_PROVINCES, US_STATES] if (country_code or "").upper() == "CA" else [US_STATES, CA_PROVINCES]
    raw = str(text).strip()
    for table in tables:
        if raw.upper() in table and len(raw) == 2:
            return raw.upper(), table[raw.upper()]
        for code, name in table.items():
            if _key(name) == _key(raw) or (code == "QC" and _key(raw) == "quebec"):
                return code, name
    return None


# ---------------------------------------------------------------------- data classes


@dataclass
class DetectedField:
    """One detected form control (or radio group / split-field part). ``descriptor`` is a short
    human label (tag, type, label or name) - never a value."""

    frame_url: str
    kind: str
    confidence: float
    descriptor: str
    element: "ElementHandle" = field(repr=False)
    control: Control
    group_index: int | None = None
    """Position of this part within a split field (phone 3-3-4, SSN, card 4x4, DOB)."""
    group_size: int | None = None
    has_value: bool = False
    frame: "Frame | None" = field(default=None, repr=False)
    info: dict[str, Any] = field(default_factory=dict, repr=False)
    members: list[tuple["ElementHandle", dict[str, Any]]] = field(default_factory=list, repr=False)
    """Radio groups: every radio with its info (labels, value)."""
    split_lengths: list[int | None] = field(default_factory=list, repr=False)

    def as_dict(self) -> dict[str, Any]:
        """Model-safe summary (kind, descriptor, control, frame, part)."""
        out: dict[str, Any] = {"kind": self.kind, "field": self.descriptor, "control": self.control,
                               "confidence": round(self.confidence, 2)}
        if self.frame is not None and self.frame.parent_frame is not None:
            out["frame"] = _origin(self.frame_url)
        if self.group_index is not None and self.group_size:
            out["part"] = f"{self.group_index + 1}/{self.group_size}"
        if self.has_value:
            out["has_value"] = True
        return out


@dataclass
class AutofillReport:
    """What :func:`autofill` did. Entries: ``{kind, field, method}`` (filled) and
    ``{kind, field, reason}`` (skipped), plus ``frame`` (origin) for iframes and ``part`` for split
    fields. Never contains values."""

    filled: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    secret_texts: list[str] = field(default_factory=list, repr=False, compare=False)
    """Private (never in :meth:`lines` / :meth:`as_dict`): the exact sensitive texts that were
    entered (formatted values, split-field parts), so the server can redact them from later page
    reads of the profile."""

    def lines(self) -> list[str]:
        out = []
        for e in self.filled:
            out.append(f"filled  {_entry_label(e)} via {e['method']}")
        for e in self.skipped:
            out.append(f"skipped {_entry_label(e)}: {e['reason']}")
        return out

    def as_dict(self) -> dict[str, Any]:
        return {"filled": list(self.filled), "skipped": list(self.skipped)}


def _entry_label(entry: dict[str, Any]) -> str:
    label = f"{entry['kind']} {entry['field']}"
    if entry.get("part"):
        label += f" (part {entry['part']})"
    if entry.get("frame"):
        label += f" [in frame {entry['frame']}]"
    return label


def _origin(url: str) -> str:
    match = re.match(r"^([a-z][a-z0-9+.-]*://[^/?#]+)", url or "", re.IGNORECASE)
    return match.group(1) if match else (url or "")[:60]


# ---------------------------------------------------------------------- detection (JS, read-only)

_SHOWN_JS = r"""
  // Can a person see `el` (at least min x min px of it)? Besides display/visibility/size, this
  // walks the ancestors (across shadow roots) for the classic autofill-phishing tricks: a tiny
  // product of opacities, clip-path / clip, clipping by an overflow:hidden (or contain:paint) box
  // that the element is positioned inside of, a zero-size scroll container, and absolutely
  // positioned fields beyond the right edge of the page.
  function shown(el, min) {
    if (!el || !el.isConnected) return false;
    const doc = el.ownerDocument, win = doc.defaultView || window;
    const r = el.getBoundingClientRect();
    if (r.width < min || r.height < min) return false;
    if (r.right + win.scrollX < 0 || r.bottom + win.scrollY < 0) return false;
    if (el.checkVisibility && !el.checkVisibility({opacityProperty: true, visibilityProperty: true})) return false;
    if (el.closest('[aria-hidden="true"],[inert]')) return false;
    const ZERO_CLIP = /^rect\(\s*0(px)?[\s,]+0(px)?[\s,]+0(px)?[\s,]+0(px)?\s*\)$/;
    const up = n => n.assignedSlot || n.parentElement || (n.parentNode && n.parentNode.host) || null;
    const makesCB = cs => cs.transform !== 'none' || cs.filter !== 'none' || cs.perspective !== 'none' ||
      /paint|strict|content|layout/.test(cs.contain || '') || /transform|filter/.test(cs.willChange || '');
    const modeOf = (pos, prev) => pos === 'fixed' ? 'fixed' : pos === 'absolute' ? 'abs' : prev;
    const root = doc.documentElement, body = doc.body;
    let box = {l: r.left, t: r.top, r: r.right, b: r.bottom}, opacity = 1, mode = 'normal', outOfFlow = false;
    for (let n = el; n && n.nodeType === 1; n = up(n)) {
      const cs = win.getComputedStyle(n);
      if (n !== el && (n.getAttribute('aria-hidden') === 'true' || n.hasAttribute('inert'))) return false;
      if (cs.display === 'contents') continue;  // no box of its own
      opacity *= parseFloat(cs.opacity || '1');
      if (opacity < 0.1) return false;
      const pos = cs.position, cp = cs.clipPath || '';
      const inset = /inset\(\s*([\d.]+)%/.exec(cp);
      if ((inset && parseFloat(inset[1]) >= 50) || /circle\(\s*0(px|%)?\s*(at|\))/.test(cp)) return false;
      if ((pos === 'absolute' || pos === 'fixed') && ZERO_CLIP.test(cs.clip || '')) return false;
      if (n === el) {
        mode = modeOf(pos, 'normal');
        outOfFlow = mode !== 'normal';
        continue;
      }
      // Does n clip el? Only if it is in el's containing-block chain.
      const isCB = mode === 'normal' || (mode === 'abs' && (pos !== 'static' || makesCB(cs))) ||
                   (mode === 'fixed' && makesCB(cs));
      if (isCB && n !== root && n !== body && cs.display !== 'inline') {
        const rr = n.getBoundingClientRect();
        const paint = /paint|strict|content/.test(cs.contain || '');
        const hideX = cs.overflowX === 'hidden' || cs.overflowX === 'clip' || paint;
        const hideY = cs.overflowY === 'hidden' || cs.overflowY === 'clip' || paint;
        if (hideX) { box.l = Math.max(box.l, rr.left); box.r = Math.min(box.r, rr.right); }
        if (hideY) { box.t = Math.max(box.t, rr.top); box.b = Math.min(box.b, rr.bottom); }
        if (box.r - box.l < 2 || box.b - box.t < 2) return false;
        const scrolls = /auto|scroll/.test(cs.overflowX + ' ' + cs.overflowY);
        if (scrolls && (rr.width < 2 || rr.height < 2)) return false;
      }
      if (isCB) {
        mode = modeOf(pos, 'normal');
        if (mode !== 'normal') outOfFlow = true;
      }
    }
    const clipsX = n => { const o = win.getComputedStyle(n).overflowX; return o === 'hidden' || o === 'clip'; };
    if (r.left + win.scrollX >= win.innerWidth && (outOfFlow || clipsX(root) || (body && clipsX(body)))) return false;
    return true;
  }
"""
"""Shared by field detection (min 2 px) and iframe checks (min 10 px)."""

_DETECT_JS = r"""(arg) => {
""" + _SHOWN_JS + r"""
  const scope = arg && arg.scope ? arg.scope : null;
  const MAX = arg && arg.max ? arg.max : 400;
  const SKIP = new Set(['hidden', 'submit', 'button', 'image', 'reset', 'file', 'search']);
  const DATE = new Set(['date', 'month', 'week', 'time', 'datetime-local']);
  const PLACEHOLDER = /^\s*(?:$|-+|—|\.\.\.|select\b|choose|please|pick|bitte|w[aä]hlen|ausw[aä]hlen|choisi|s[eé]lection|selecci|elige|seleccione|escoja)/i;
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const textOf = (node, limit = 160) => {
    let out = '';
    const walk = n => {
      if (out.length > limit) return;
      if (n.nodeType === 3) { out += n.nodeValue + ' '; return; }
      if (n.nodeType !== 1) return;
      const t = n.tagName;
      if (t === 'INPUT' || t === 'SELECT' || t === 'TEXTAREA' || t === 'SCRIPT' || t === 'STYLE' ||
          t === 'OPTION' || t === 'BUTTON' || t === 'TEMPLATE' || t === 'NOSCRIPT') return;
      for (const c of n.childNodes) walk(c);
    };
    walk(node);
    return clean(out).slice(0, limit);
  };
  const byIds = (el, attr) => {
    const ids = (el.getAttribute(attr) || '').split(/\s+/).filter(Boolean);
    if (!ids.length) return '';
    const root = el.getRootNode();
    return clean(ids.map(id => {
      const n = root.getElementById ? root.getElementById(id) : document.getElementById(id);
      return n ? textOf(n) : '';
    }).join(' '));
  };
  const isControl = el => {
    const t = el.tagName;
    if (t === 'INPUT') return !SKIP.has((el.type || 'text').toLowerCase());
    if (t === 'SELECT') return !el.multiple;
    if (t === 'TEXTAREA') return true;
    return !!el.isContentEditable && el.hasAttribute('contenteditable') &&
           !(el.parentElement && el.parentElement.isContentEditable);
  };
  const isFormish = n => n.nodeType === 1 && (isControl(n) || !!n.querySelector('input:not([type=hidden]),select,textarea'));
  const visible = el => shown(el, 2);
  const nearby = el => {
    const parts = [];
    let len = 0, n = el, steps = 0;
    while (n && len < 80 && steps < 40) {
      steps++;
      if (n.previousSibling) {
        n = n.previousSibling;
        if (n.nodeType === 1) {
          if (isFormish(n)) break;
          const t = textOf(n);
          if (t) { parts.unshift(t); len += t.length; }
        } else if (n.nodeType === 3) {
          const t = clean(n.nodeValue);
          if (t) { parts.unshift(t); len += t.length; }
        }
      } else {
        n = n.parentNode;
        if (!n || n.nodeType !== 1 || n.tagName === 'FORM' || n.tagName === 'BODY' || n.tagName === 'FIELDSET') break;
      }
    }
    return clean(parts.join(' ')).slice(-80);
  };
  const after = el => {
    const parts = [];
    let n = el.nextSibling, steps = 0;
    while (n && steps < 6) {
      steps++;
      if (n.nodeType === 1 && isFormish(n)) break;
      const t = n.nodeType === 3 ? clean(n.nodeValue) : n.nodeType === 1 ? textOf(n) : '';
      if (t) parts.push(t);
      n = n.nextSibling;
    }
    return clean(parts.join(' ')).slice(0, 60);
  };
  const legend = el => {
    let s = '';
    const fs = el.closest('fieldset');
    if (fs) { const lg = fs.querySelector(':scope > legend'); if (lg) s = textOf(lg, 80); }
    const g = el.closest('[role=group],[role=radiogroup]');
    if (g) s = clean(s + ' ' + (g.getAttribute('aria-label') || byIds(g, 'aria-labelledby')));
    return s.slice(0, 120);
  };
  const controls = [];
  const add = el => { if (controls.length < MAX && isControl(el)) controls.push(el); };
  const visit = root => {
    for (const el of root.querySelectorAll('*')) {
      add(el);
      if (el.shadowRoot) visit(el.shadowRoot);
    }
  };
  if (scope) { add(scope); visit(scope); if (scope.shadowRoot) visit(scope.shadowRoot); }
  else visit(document);
  const forms = [], boxes = [];
  const idx = (arr, x) => { if (!x) return -1; let i = arr.indexOf(x); if (i < 0) { arr.push(x); i = arr.length - 1; } return i; };
  const infos = controls.map((el, order) => {
    const tag = el.tagName.toLowerCase();
    const type = tag === 'input' ? (el.type || 'text').toLowerCase() : '';
    const control = tag === 'select' ? 'select' : tag === 'textarea' ? 'text' : tag !== 'input' ? 'contenteditable'
      : DATE.has(type) ? 'date' : type === 'radio' ? 'radio' : type === 'checkbox' ? 'checkbox' : 'text';
    const labelEls = el.labels ? [...el.labels] : [];
    const choice = control === 'radio' || control === 'checkbox';
    let hasValue = false;
    if (control === 'select') {
      const o = el.selectedIndex >= 0 ? el.options[el.selectedIndex] : null;
      hasValue = !!o && el.value !== '' && !PLACEHOLDER.test(o.text) && (el.selectedIndex > 0 || o.defaultSelected);
    } else if (choice) hasValue = !!el.checked;
    else if (control === 'contenteditable') hasValue = clean(el.innerText) !== '';
    else hasValue = (el.value || '').trim() !== '';
    const inputVisible = visible(el);
    const r = el.getBoundingClientRect();
    const parent = el.parentElement;
    return {
      order, tag, type, control,
      autocomplete: clean(el.getAttribute('autocomplete')).toLowerCase(),
      name: el.getAttribute('name') || '', id: el.id || '',
      placeholder: clean(el.getAttribute('placeholder') || el.getAttribute('data-placeholder')),
      aria: clean(el.getAttribute('aria-label')), labelledby: byIds(el, 'aria-labelledby'),
      label: clean(labelEls.map(l => textOf(l)).join(' ')), title: clean(el.getAttribute('title')),
      nearby: nearby(el), legend: legend(el), after: choice ? after(el) : '',
      inputmode: (el.getAttribute('inputmode') || '').toLowerCase(),
      maxLength: (tag === 'input' || tag === 'textarea') && el.maxLength > 0 ? el.maxLength : null,
      size: el.hasAttribute('size') && tag === 'input' ? (parseInt(el.getAttribute('size'), 10) || null) : null,
      pattern: el.getAttribute('pattern') || '',
      choiceValue: choice ? (el.value || '') : '',
      visible: choice ? (inputVisible || labelEls.some(visible)) : inputVisible,
      inputVisible,
      disabled: el.matches(':disabled') || el.getAttribute('aria-disabled') === 'true',
      readonly: !!el.readOnly || el.getAttribute('aria-readonly') === 'true',
      hasValue, checked: !!el.checked,
      options: tag === 'select' ? [...el.options].slice(0, 600).map(o => [o.value, clean(o.label || o.text), o.disabled]) : null,
      selectedIndex: tag === 'select' ? el.selectedIndex : -1,
      radioName: control === 'radio' ? (el.name || '') : '',
      radioGroup: control === 'radio' ? idx(boxes, el.closest('[role=radiogroup],fieldset') || parent) : -1,
      form: idx(forms, el.form || el.closest('form')),
      parent: idx(boxes, parent), grand: idx(boxes, parent && parent.parentElement),
      rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
    };
  });
  return {els: controls, infos, lang: (document.documentElement.lang || '').toLowerCase()};
}"""

_FRAME_VISIBLE_JS = "el => {" + _SHOWN_JS + "\n  return shown(el, 10);\n}"
"""An ``<iframe>`` must be at least 10x10 px and pass the same checks as a field (a cross-origin
child frame cannot see that its own element is hidden, so every iframe up the chain is checked)."""

_HIT_JS = r"""(el) => {
  // Scroll the field into the middle of its viewport and check that a click there would reach it
  // (or one of its labels): overlays that cover a field are invisible to the style checks.
  if (!el.isConnected) throw new Error('Element is not attached to the DOM');  // re-rendered: try again
  el.scrollIntoView({block: 'center', inline: 'nearest'});
  const doc = el.ownerDocument, win = doc.defaultView || window;
  const r = el.getBoundingClientRect();
  const labels = el.labels ? [...el.labels] : [];
  const ok = h => !!h && (h === el || el.contains(h) || labels.some(l => l === h || l.contains(h)));
  const at = (x, y) => {
    let h = doc.elementFromPoint(x, y);
    while (h && h.shadowRoot) {
      const inner = h.shadowRoot.elementFromPoint(x, y);
      if (!inner || inner === h) break;
      h = inner;
    }
    return h;
  };
  for (const [fx, fy] of [[0.5, 0.5], [0.25, 0.5], [0.75, 0.5], [0.9, 0.5], [0.5, 0.25], [0.5, 0.75]]) {
    const x = r.left + r.width * fx, y = r.top + r.height * fy;
    if (x < 0 || y < 0 || x >= win.innerWidth || y >= win.innerHeight) continue;
    if (ok(at(x, y))) return [x, y];
  }
  return null;
}"""
"""Returns the reachable point (viewport coordinates of the element's frame), or null."""

_FRAME_HIT_JS = r"""(fe, point) => {
  // Is the <iframe> itself reachable at `point` (coordinates inside the frame's viewport)?
  if (!fe.isConnected) throw new Error('Element is not attached to the DOM');
  const doc = fe.ownerDocument, win = doc.defaultView || window;
  const place = () => {
    const r = fe.getBoundingClientRect(), cs = win.getComputedStyle(fe);
    return [r.left + fe.clientLeft + parseFloat(cs.paddingLeft || '0') + point[0],
            r.top + fe.clientTop + parseFloat(cs.paddingTop || '0') + point[1]];
  };
  let [x, y] = place();
  if (x < 0 || y < 0 || x >= win.innerWidth || y >= win.innerHeight) {
    fe.scrollIntoView({block: 'center', inline: 'nearest'});
    [x, y] = place();
  }
  if (x < 0 || y < 0 || x >= win.innerWidth || y >= win.innerHeight) return null;
  let h = doc.elementFromPoint(x, y);
  while (h && h.shadowRoot) {
    const inner = h.shadowRoot.elementFromPoint(x, y);
    if (!inner || inner === h) break;
    h = inner;
  }
  return h === fe ? [x, y] : null;
}"""

_OPTIONS_JS = "e => [...e.options].slice(0, 600).map(o => [o.value, (o.label || o.text).replace(/\\s+/g, ' ').trim(), o.disabled])"


@dataclass
class _Raw:
    frame: "Frame"
    frame_url: str
    frame_index: int
    lang: str
    element: "ElementHandle"
    info: dict[str, Any]
    kind: str | None = None
    confidence: float = 0.0


async def _frames_to_scan(page: "Page", scope: "Locator | None") -> list[tuple["Frame", "ElementHandle | None"]]:
    """``(frame, scope element in that frame or None)`` for every frame to scan, main frame first.
    Child frames whose ``<iframe>`` element is invisible are skipped."""
    from .driver import Error as PlaywrightError

    scope_handle = scope_frame = None
    if scope is not None:
        scope_handle = await scope.element_handle(timeout=ACTION_TIMEOUT_MS)
        scope_frame = await scope_handle.owner_frame()
    out: list[tuple[Frame, ElementHandle | None]] = []
    for frame in page.frames:
        if frame.is_detached():
            continue
        if scope_frame is not None and frame == scope_frame:
            out.append((frame, scope_handle))
            continue
        if frame.parent_frame is None:
            if scope_frame is None:
                out.append((frame, None))
            continue
        try:
            # every <iframe> on the way up must be visible (and, with a scope, inside it)
            inside = scope_frame is None
            child, ok = frame, True
            while child.parent_frame is not None:
                element = await child.frame_element()
                if not await element.evaluate(_FRAME_VISIBLE_JS):
                    ok = False
                    break
                if scope_frame is not None and child.parent_frame == scope_frame:
                    inside = await scope_handle.evaluate("(s, f) => s.contains(f)", element)  # type: ignore[union-attr]
                child = child.parent_frame
            if ok and inside:
                out.append((frame, None))
        except PlaywrightError:
            continue  # frame went away meanwhile
    return out


async def _scan(page: "Page", scope: "Locator | None") -> list[_Raw]:
    from .content import frame_url
    from .driver import Error as PlaywrightError

    raws: list[_Raw] = []
    frames = await _frames_to_scan(page, scope)
    try:
        for index, (frame, scope_handle) in enumerate(frames):
            try:
                result = await frame.evaluate_handle(_DETECT_JS, {"scope": scope_handle,
                                                                  "max": MAX_CONTROLS_PER_FRAME})
                try:
                    data = await result.evaluate("r => ({infos: r.infos, lang: r.lang})")
                    elements_handle = await result.get_property("els")
                    props = await elements_handle.get_properties()
                    await elements_handle.dispose()
                finally:
                    await result.dispose()
            except PlaywrightError as exc:  # the frame navigated or went away meanwhile
                log.debug("autofill: frame not scanned (%s)", str(exc).splitlines()[0][:120] if str(exc) else exc)
                continue
            url = await frame_url(frame)
            elements: dict[int, Any] = {int(k): v for k, v in props.items() if k.isdigit()}
            for i, info in enumerate(data["infos"]):
                handle = elements.get(i)
                element = handle.as_element() if handle is not None else None
                if element is None:
                    continue
                raws.append(_Raw(frame, url, index, data.get("lang") or "", element, info))
    finally:
        for _, scope_handle in frames:
            if scope_handle is not None:
                await scope_handle.dispose()
    return raws


async def detect_fields(page: "Page", *, scope: "Locator | None" = None) -> list[DetectedField]:
    """Detected fillable fields of ``page`` (all visible frames, or only inside ``scope``), in
    document order per frame. Read-only: nothing is typed and the DOM is not modified.
    Call :func:`dispose_fields` when the element handles are no longer needed."""
    raws = await _scan(page, scope)
    fields = classify_raw(raws)
    used = {id(f.element) for f in fields} | {id(m[0]) for f in fields for m in f.members}
    await asyncio.gather(*(r.element.dispose() for r in raws if id(r.element) not in used), return_exceptions=True)
    return fields


async def dispose_fields(fields: Iterable[DetectedField]) -> None:
    """Release the element handles held by ``fields``."""
    handles = []
    for f in fields:
        handles.append(f.element)
        handles.extend(m[0] for m in f.members if m[0] is not f.element)
    await asyncio.gather(*(h.dispose() for h in handles), return_exceptions=True)


# ---------------------------------------------------------------------- classification (pure Python)

_AC_MAP = {
    "given-name": "first_name", "additional-name": "middle_name", "family-name": "last_name", "name": "full_name",
    "email": "email", "username": "username", "new-password": "password", "current-password": "password",
    "tel": "phone", "tel-national": "phone_national", "tel-country-code": "phone_country_code",
    "tel-area-code": "phone_area", "tel-local": "phone_local", "tel-local-prefix": "phone_prefix",
    "tel-local-suffix": "phone_suffix", "organization": "company", "street-address": "street",
    "address-line1": "street", "address-line2": "address_line2", "address-level1": "state",
    "address-level2": "city", "postal-code": "postal_code", "country": "country", "country-name": "country",
    "bday": "birth_date", "bday-day": "birth_day", "bday-month": "birth_month", "bday-year": "birth_year",
    "sex": "gender", "cc-name": "card_name", "cc-given-name": "first_name", "cc-family-name": "last_name",
    "cc-number": "card_number", "cc-exp": "card_exp", "cc-exp-month": "card_exp_month",
    "cc-exp-year": "card_exp_year", "cc-csc": "card_cvv", "cc-type": "card_type",
}
_AC_IGNORE = frozenset({
    "honorific-prefix", "honorific-suffix", "nickname", "organization-title", "address-line3",
    "address-level3", "address-level4", "cc-additional-name", "transaction-currency", "transaction-amount",
    "language", "url", "photo", "impp", "one-time-code", "tel-extension", "webauthn",
})
_AC_MODIFIERS = frozenset({"shipping", "billing", "home", "work", "mobile"})
_AC_SKIP_MODIFIERS = frozenset({"fax", "pager"})


def autocomplete_kind(value: str) -> tuple[str | None, bool]:
    """``(kind, authoritative)`` from an ``autocomplete`` attribute. ``authoritative`` without a
    kind means "a known token for something we never fill" (``one-time-code``, ``fax tel``...)."""
    tokens = [t for t in (value or "").lower().split() if not t.startswith("section-")]
    if not tokens:
        return None, False
    if any(t in _AC_SKIP_MODIFIERS for t in tokens):
        return None, True
    tokens = [t for t in tokens if t not in _AC_MODIFIERS]
    if not tokens:
        return None, False
    token = tokens[-1]
    if token in _AC_IGNORE:
        return None, True
    return _AC_MAP.get(token), token in _AC_MAP


def _p(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


_IGNORE_RE = _p(r"(place of birth|birth ?place|geburtsort|geburtsland|lieu de naissance|lugar de nacimiento|"
                r"country of birth|birth ?country|maiden name|geburtsname|nom de naissance|captcha|coupon|promo|"
                r"gift ?card|voucher|gutschein|referral|discount|rabatt|search|suche|(sms|one ?time|otp|2fa|two ?factor|"
                r"email|e-mail) ?(verification )?code|\bpin\b)")
_CVV_RE = _p(r"\b(cvv2?|cvc2?|csc|cvn|ccv|cid|security ?code|card ?code|card ?verification|verification ?(value|number)|"
             r"kartenprufnummer|kartenpruf\w*|prufnummer|prufziffer|sicherheitscode|cryptogramme|code de securite|"
             r"codigo de seguridad|card ?security)\b")
_CARD_NAME_RE = _p(r"(name on (the )?card|card ?holder|cardholder|holder ?name|nameoncard|name ?on ?card|cc ?name|"
                   r"karteninhaber\w*|\binhaber\b|titulaire|\btitular\b|nom (sur la carte|du titulaire)|"
                   r"nombre (en la tarjeta|del titular))")
_CARD_TYPE_RE = _p(r"\b(card ?type|card ?brand|cc ?type|kartentyp|kartenart|type de carte|tipo de tarjeta)\b")
_CARD_NUMBER_RE = _p(r"(card ?(number|no|num|nr)\b|card ?#|cardnumber|cc ?(number|num|no)\b|ccnum|credit ?card|"
                     r"debit ?card|kartennummer|kreditkarten?(nummer)?|numero de (la )?carte|numero de (la )?tarjeta|"
                     r"\bpan\b)")
_EXP_RE = _p(r"\b(exp|expiry|expiration|expires|expire|expiration date|exp date|cc ?exp\w*|valid (thru|through|until|to)|"
             r"gultig bis|gultigkeit|ablaufdatum|ablauf\w*|date d.?expiration|date de validite|fecha de (caducidad|"
             r"vencimiento|expiracion)|vencimiento|caducidad)\b")
_MMYY_RE = _p(r"\bmm ?/? ?(yy|yyyy|jj|jjjj|aa|aaaa)\b")
"""An ``MM/YY`` placeholder means a card expiry - unless a day token is there too (``TT.MM.JJJJ``)."""
_CARD_CTX_RE = _p(r"(card|\bcc\b|kredit|karte|carte|tarjeta)")
_SSN_RE = _p(r"\b(ssn|social ?security|soc ?sec|sozialversicherungs?(nummer)?|versicherungsnummer|securite sociale|"
             r"seguro social)\b")
_PASSWORD_RE = _p(r"\b(pass ?word|passwort|kennwort|mot de passe|contrasena|passwd|pwd)\b")
_EMAIL_RE = _p(r"(e ?-?mail|courriel|correo|mail ?address|\bmail\b)")
_USERNAME_RE = _p(r"\b(user ?name|user ?id|login( ?name| ?id)?|benutzer ?name|benutzer|nom d.?utilisateur|"
                  r"identifiant|nombre de usuario|usuario|account ?name|screen ?name)\b")
_COMPANY_RE = _p(r"\b(company|organi[sz]ation|business ?name|employer|firma|firmenname|unternehmen|societe|"
                 r"entreprise|empresa|compania)\b")
_BIRTH_RE = _p(r"(birth|\bdob\b|bday|geburt\w*|naissance|nacimiento|fecha de nac\w*|date of b)")
_GENDER_RE = _p(r"\b(gender|sex|geschlecht|sexe|genero|sexo|anrede|salutation|civilite)\b")
_PHONE_RE = _p(r"(phone|\btel\b|telephone|mobile|\bcell\b|cellular|handy|telefon\w*|mobil\w*|rufnummer|portable|"
               r"telefono|movil|celular|whats ?app)")
_LINE2_RE = _p(r"(address ?(line)? ?2\b|addr ?2\b|street ?2\b|line ?2\b|\bapt\b|apartment|\bsuite\b|\bunit\b|"
               r"building|\bfloor\b|adresszusatz|\bzusatz\b|complement|\bpiso\b|departamento|\bdpto\b)")
_STREET_RE = _p(r"(street|address|\baddr\b|addr ?1|\broad\b|strasse|\bstr\b|anschrift|adresse|\brue\b|"
                r"direccion|\bcalle\b|domicilio)")
_HOUSE_NUMBER_RE = _p(r"\b(hausnummer|hausnr|haus ?nr|house ?(number|no|nr|num)|huisnummer|huisnr|"
                      r"street ?(number|no|nr|num)|numero de (la )?(rue|calle|casa))\b")
"""A separate house-number field (German "Straße" + "Hausnr."); "Straße und Hausnummer" stays a street."""
_CITY_RE = _p(r"\b(city|town|suburb|locality|ort|stadt|wohnort|ville|localite|commune|ciudad|localidad|municipio|"
              r"poblacion)\b")
_STATE_RE = _p(r"\b(state|province|region|county|territory|prefecture|bundesland|kanton|departement|provincia|"
               r"estado|comunidad)\b")
_POSTAL_RE = _p(r"\b(zip( ?code)?|zipcode|postal( ?code)?|post ?code|postcode|plz|postleitzahl|code postal|"
                r"codigo postal|cp)\b")
_COUNTRY_RE = _p(r"\b(country|nation|land|pays|pais)\b")
_MIDDLE_RE = _p(r"\b(middle ?(name|initial)|mname|zweiter vorname|segundo nombre|deuxieme prenom)\b")
_FULL_NAME_RE = _p(r"\b(full ?name|fullname|complete name|vollstandiger name|nom complet|nombre completo|"
                   r"nombre y apellidos?|your name)\b")
_FIRST_RE = _p(r"\b(first ?name|firstname|given ?name|fname|forename|vorname|prenom|first|nombre|primer nombre)\b")
_LAST_RE = _p(r"\b(last ?name|lastname|family ?name|surname|lname|nachname|familienname|zuname|nom de famille|"
              r"apellidos?|last)\b")
_NAME_RE = _p(r"\b(name|nom|nombre)\b")
_NOT_PERSON_NAME_RE = _p(r"\b(pet|product|event|project|team|group|file|domain|host|device|server|display|item|list|"
                         r"page|site|app|channel|room|shop|store|brand|model|course|school|university|bank|campaign|"
                         r"job|position|role|title|tag|folder|wifi|network) ?name\b")
"""Generic "... name" fields that are not a person's name."""
_DAY_RE = _p(r"\b(day|dd|tag|tt|jour|jj|dia)\b")
_MONTH_RE = _p(r"\b(month|mm|mon|monat|mois|mes)\b")
_YEAR_RE = _p(r"\b(year|yyyy|yy|yr|jahr|jjjj|annee|aaaa|aa|ano)\b")

_ORDERED: list[tuple[str, re.Pattern[str]]] = [
    # card fields first, most specific first: "Credit or debit card expiration date" is an expiry
    ("card_cvv", _CVV_RE), ("card_exp*", _EXP_RE), ("card_name", _CARD_NAME_RE), ("card_type", _CARD_TYPE_RE),
    ("card_number", _CARD_NUMBER_RE), ("ssn", _SSN_RE), ("password", _PASSWORD_RE),
    ("email", _EMAIL_RE), ("username", _USERNAME_RE), ("company", _COMPANY_RE), ("birth*", _BIRTH_RE),
    ("gender", _GENDER_RE), ("phone", _PHONE_RE), ("address_line2", _LINE2_RE),
    ("house_number", _HOUSE_NUMBER_RE), ("street", _STREET_RE),
    # country before state: "Country/Region" is a country
    ("city", _CITY_RE), ("country", _COUNTRY_RE), ("state", _STATE_RE), ("postal_code", _POSTAL_RE),
    ("middle_name", _MIDDLE_RE), ("full_name", _FULL_NAME_RE), ("first_name", _FIRST_RE),
    ("last_name", _LAST_RE), ("name_generic", _NAME_RE),
]


def _js_regex(rx: re.Pattern[str]) -> str:
    return "new RegExp(" + json.dumps(rx.pattern) + ")"


SENSITIVE_FIELD_JS = (
    r"""el => {
  // Does this control hold a card number, expiry, CVV, SSN, password or one-time code? Uses the same
  // expressions as detect_fields (generated from them), over the same folded label text.
  if ((el.type || '').toLowerCase() === 'password') return true;
  const AC = new Set(['cc-number', 'cc-csc', 'cc-exp', 'cc-exp-month', 'cc-exp-year', 'new-password',
                      'current-password', 'one-time-code']);
  const tokens = (el.getAttribute('autocomplete') || '').toLowerCase().split(/\s+/);
  if (tokens.some(t => AC.has(t))) return true;
  const words = s => String(s || '').replace(/([a-z])([A-Z])/g, '$1 $2').replace(/([A-Za-z])(\d)/g, '$1 $2')
    .replace(/[_\-.\[\]:()*\/\\]+/g, ' ').normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase()
    .replace(/\s+/g, ' ').trim();
  const root = el.getRootNode();
  const byIds = attr => (el.getAttribute(attr) || '').split(/\s+/).filter(Boolean)
    .map(id => { const n = root.getElementById ? root.getElementById(id) : null; return n ? n.textContent : ''; })
    .join(' ');
  const labels = el.labels ? [...el.labels].map(l => l.textContent).join(' ') : '';
  const text = [labels, el.getAttribute('aria-label'), byIds('aria-labelledby'), el.getAttribute('placeholder'),
                el.getAttribute('title'), el.getAttribute('name'), el.id].map(words).filter(Boolean).join(' | ');
  const SECRET = ["""
    + ", ".join(_js_regex(rx) for rx in (_CVV_RE, _CARD_NUMBER_RE, _SSN_RE, _PASSWORD_RE))
    + "];\n  const EXP = " + _js_regex(_EXP_RE) + ", CARD = " + _js_regex(_CARD_CTX_RE)
    + ", MMYY = " + _js_regex(_MMYY_RE) + r""";
  return SECRET.some(rx => rx.test(text)) || (EXP.test(text) && CARD.test(text)) || MMYY.test(text);
}"""
)
"""``el => bool``: is ``el`` a sensitive field? Used to mask such values in page snapshots
(:func:`profilepilot.automation.content.snapshot`); generated from the detection expressions so
the two cannot drift apart."""


def _words(text: str) -> str:
    """Signal text for regexes: camelCase / snake_case / digits split, folded."""
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text or "")
    text = re.sub(r"([A-Za-z])(\d)", r"\1 \2", text)
    text = re.sub(r"[_\-.\[\]:()*/\\]+", " ", text)
    return _fold(text)


def _strong_text(info: dict[str, Any]) -> str:
    parts = [info.get(k) or "" for k in ("label", "aria", "labelledby", "placeholder", "title", "name", "id")]
    return " | ".join(_words(p) for p in parts if p)


def _context_text(info: dict[str, Any]) -> str:
    return " | ".join(_words(info.get(k) or "") for k in ("nearby", "legend") if info.get(k))


def _date_part(text: str) -> str | None:
    hits = [part for part, rx in (("day", _DAY_RE), ("month", _MONTH_RE), ("year", _YEAR_RE)) if rx.search(text)]
    return hits[0] if len(hits) == 1 else None


def options_shape(options: list[list[Any]] | None) -> str | None:
    """``"day"``, ``"month"`` or ``"year"`` when a select's real options look like that."""
    if not options:
        return None
    real = [(v, t) for v, t in _real_options(options)]
    if len(real) < 2:
        return None
    month_keys = {_key(n) for row in _MONTHS for n in row}
    if sum(1 for _, t in real if _key(t) in month_keys or any(w in month_keys for w in _fold(t).split())) >= 10:
        return "month"
    nums = []
    for v, t in real:
        m = re.fullmatch(r"\s*(\d{1,4})\s*", t) or re.fullmatch(r"\s*(\d{1,4})\s*", v)
        if not m:
            return None
        nums.append(int(m.group(1)))
    if all(1900 <= n <= 2100 for n in nums) and len(nums) >= 3:
        return "year"
    if all(1 <= n <= 12 for n in nums) and len(nums) == 12:
        return "month"
    if all(1 <= n <= 31 for n in nums) and 28 <= len(nums) <= 31:
        return "day"
    return None


def _real_options(options: Iterable[Any]) -> list[tuple[str, str]]:
    """``(value, label)`` of the options a person could meaningfully pick. An empty value marks a
    placeholder ("Select...", "Day"): without a value attribute the DOM reports the label as the
    value, so an empty value is always deliberate."""
    out = []
    for option in options:
        value, label = str(option[0]), str(option[1])
        disabled = len(option) > 2 and bool(option[2])
        if value.strip() and not disabled and not (_PLACEHOLDER_OPTION.match(label) and _key(value) == _key(label)):
            out.append((value, label))
    return out


def _match_rules(text: str, info: dict[str, Any]) -> str | None:
    """First matching kind for one signal text (``*`` kinds need refining by the caller)."""
    if not text:
        return None
    if _IGNORE_RE.search(text):
        return "ignore"
    for kind, rx in _ORDERED:
        if rx.search(text):
            if kind == "house_number" and _STREET_RE.search(_HOUSE_NUMBER_RE.sub(" ", text)):
                return "street"  # "Straße und Hausnummer", "Street and house number": one field
            return kind
        if kind == "card_exp*" and _MMYY_RE.search(text) and not _DAY_RE.search(text):
            return kind
    return None


def classify(info: dict[str, Any]) -> tuple[str | None, float]:
    """``(kind, confidence)`` for one control's signals (see the module docstring); kind None when
    the control is not something an identity can fill. ``name_generic`` is resolved per form by
    :func:`classify_raw`."""
    control = info.get("control")
    if control == "checkbox":
        return None, 0.0
    ac_kind, authoritative = autocomplete_kind(info.get("autocomplete") or "")
    if ac_kind:
        return ac_kind, 1.0
    if authoritative:
        return None, 0.0
    typ = info.get("type") or ""
    strong = _strong_text(info)
    context = _context_text(info)
    if typ == "email":
        return "email", 0.95
    if typ == "password":
        return ("password", 0.95) if not _IGNORE_RE.search(strong) else (None, 0.0)
    if control == "radio":
        # A radio's own label is one *option* ("Female", "Other"); the group's label (legend,
        # name, preceding text) says what is asked. Option labels count in _radio_group_kind.
        group_text = " | ".join(_words(info.get(k) or "") for k in ("legend", "name", "nearby") if info.get(k))
        kind = _match_rules(group_text, info)
        return (kind, 0.85) if kind in ("gender", "card_type") else (None, 0.0)

    for from_context, text, confidence in ((False, strong, 0.85), (True, context, 0.6)):
        kind = _match_rules(text, info)
        if kind == "ignore":
            return None, 0.0
        if kind is None or (control == "date" and kind not in ("card_exp*", "birth*")):
            continue  # a date input is only ever a birth date or a card expiry
        if kind in ("name_generic", "full_name") and _NOT_PERSON_NAME_RE.search(text):
            return None, 0.0
        both = f"{strong} | {context}"
        if kind == "card_exp*":
            if typ == "month" or control == "date":
                return ("card_exp", confidence) if typ == "month" else (None, 0.0)
            part = _date_part(strong) or (_options_shape_part(info))
            return {"month": "card_exp_month", "year": "card_exp_year"}.get(part or "", "card_exp"), confidence
        if kind == "birth*":
            if control == "date":
                return ("birth_date", confidence) if typ == "date" else (None, 0.0)
            part = _date_part(strong) or _options_shape_part(info)
            if part:
                return f"birth_{part}", confidence
            return "birth_date", confidence
        if kind == "gender" and control == "text" and not re.search(r"\b(gender|sex|geschlecht|sexe|genero|sexo)\b", strong):
            return None, 0.0  # "salutation"/"title" text inputs are not genders
        if from_context and kind in ("first_name", "last_name", "name_generic", "full_name", "middle_name") \
                and _date_part(strong):
            continue  # an unlabeled day/month/year control after a name field
        if kind in ("state", "country", "city") and _BIRTH_RE.search(both):
            return None, 0.0
        return kind, confidence

    # date parts without a label of their own: birth or card context, or the options' shape
    part = _date_part(strong) or _options_shape_part(info)
    if part and control in ("select", "text"):
        if _BIRTH_RE.search(context):
            return f"birth_{part}", 0.6
        if _EXP_RE.search(context) or _CARD_CTX_RE.search(context):
            if part in ("month", "year"):
                return f"card_exp_{part}", 0.6
    if typ == "tel":
        return "phone", 0.7
    return None, 0.0


def _options_shape_part(info: dict[str, Any]) -> str | None:
    return options_shape(info.get("options")) if info.get("control") == "select" else None


def _radio_label(info: dict[str, Any]) -> str:
    return info.get("label") or info.get("aria") or info.get("after") or info.get("choiceValue") or ""


def _descriptor(info: dict[str, Any]) -> str:
    tag, typ, control = info.get("tag"), info.get("type") or "", info.get("control")
    if control == "contenteditable":
        head = "contenteditable"
    elif tag == "input":
        head = "input" if typ in ("", "text") else f"input[type={typ}]"
    else:
        head = str(tag)
    label = ""
    for key in ("label", "aria", "labelledby", "placeholder", "title", "nearby", "name", "id", "legend"):
        text = " ".join(str(info.get(key) or "").split())
        if text and (key != "nearby" or re.search(r"[^\W\d_]{2}", text)):  # not just ")" or "/"
            label = text
            break
    if len(label) > 40:
        label = label[:39] + "…"
    return f'{head} "{label}"' if label else head


_FAMILY = {"phone": "phone", "ssn": "ssn", "card_number": "card", "birth_date": "birth",
           "birth_day": "birth", "birth_month": "birth", "birth_year": "birth"}
_DEFAULT_SPLITS = {"phone": {2: [3, 7], 3: [3, 3, 4]}, "ssn": {3: [3, 2, 4]}, "card": {}}
_DMY_LANGS = ("de", "fr", "es", "it", "pt", "nl", "pl", "en-gb", "en-au", "en-ie", "en-nz", "en-in", "en-za")


def _small(info: dict[str, Any]) -> bool:
    n = info.get("maxLength") or info.get("size")
    return bool(n) and 1 <= int(n) <= 5


def classify_raw(raws: list[_Raw]) -> list[DetectedField]:
    """Classify scanned controls, group radios and split fields, resolve generic names."""
    usable = [r for r in raws if r.info.get("visible") and not r.info.get("disabled") and not r.info.get("readonly")]
    for r in usable:
        r.kind, r.confidence = classify(r.info)
    _resolve_names(usable)
    _resolve_house_numbers(usable)
    fields: list[DetectedField] = []
    split_done: set[int] = set()
    radio_groups: dict[tuple[int, int, str, int], list[_Raw]] = {}
    for i, r in enumerate(usable):
        if r.info.get("control") == "radio":
            key = (r.frame_index, r.info.get("form", -1), r.info.get("radioName") or "",
                   -1 if r.info.get("radioName") else r.info.get("radioGroup", -1))
            radio_groups.setdefault(key, []).append(r)
    emitted_groups: set[tuple[int, int, str, int]] = set()
    for i, r in enumerate(usable):
        info = r.info
        if info.get("control") == "radio":
            key = (r.frame_index, info.get("form", -1), info.get("radioName") or "",
                   -1 if info.get("radioName") else info.get("radioGroup", -1))
            if key in emitted_groups:
                continue
            emitted_groups.add(key)
            group = radio_groups[key]
            kind, confidence = _radio_group_kind(group)
            if kind:
                first = group[0]
                label = first.info.get("legend") or first.info.get("radioName") or _radio_label(first.info)
                label = " ".join(str(label).split())
                label = label[:39] + "…" if len(label) > 40 else label
                fields.append(DetectedField(
                    frame_url=first.frame_url, kind=kind, confidence=confidence,
                    descriptor=f'radio group "{label}"' if label else "radio group", element=first.element,
                    control="radio", has_value=any(m.info.get("checked") for m in group), frame=first.frame,
                    info=first.info, members=[(m.element, m.info) for m in group]))
            continue
        if i in split_done:
            continue
        family = _FAMILY.get(r.kind or "")
        if family and info.get("control") == "text" and _small(info):
            members = [r]
            j = i + 1
            while j < len(usable) and _joins(usable[j], members[-1], family):
                members.append(usable[j])
                j += 1
            if len(members) >= 2:
                split_done.update(range(i, i + len(members)))
                fields.extend(_split_fields(members, family))
                continue
        if not r.kind or r.kind == "name_generic":
            continue
        fields.append(_field(r, r.kind, r.confidence))
    return fields


def _field(r: _Raw, kind: str, confidence: float, *, group_index: int | None = None,
           group_size: int | None = None, lengths: list[int | None] | None = None) -> DetectedField:
    return DetectedField(
        frame_url=r.frame_url, kind=kind, confidence=confidence, descriptor=_descriptor(r.info),
        element=r.element, control=r.info.get("control") or "text", group_index=group_index,
        group_size=group_size, has_value=bool(r.info.get("hasValue")), frame=r.frame,
        info={**r.info, "lang": r.lang}, split_lengths=lengths or [])


def _joins(candidate: _Raw, prev: _Raw, family: str) -> bool:
    ci, pi = candidate.info, prev.info
    if candidate.frame_index != prev.frame_index or ci.get("form") != pi.get("form"):
        return False
    if ci.get("order") != pi.get("order", -2) + 1 or ci.get("control") != "text" or not _small(ci):
        return False
    if candidate.kind and _FAMILY.get(candidate.kind) != family:
        return False
    return (ci.get("parent") == pi.get("parent") or ci.get("grand") == pi.get("grand")
            or ci.get("parent") == pi.get("grand") or ci.get("grand") == pi.get("parent"))


def _split_fields(members: list[_Raw], family: str) -> list[DetectedField]:
    size = len(members)
    lengths = [m.info.get("maxLength") or m.info.get("size") for m in members]
    out = []
    if family == "birth":
        parts = _birth_parts(members)
        for index, (m, part) in enumerate(zip(members, parts)):
            out.append(_field(m, f"birth_{part}", 0.75, group_index=index, group_size=size, lengths=lengths))
        return out
    kind = {"phone": "phone", "ssn": "ssn", "card": "card_number"}[family]
    for index, m in enumerate(members):
        out.append(_field(m, kind, 0.8, group_index=index, group_size=size, lengths=lengths))
    return out


def _birth_parts(members: list[_Raw]) -> list[str]:
    explicit = []
    for m in members:
        part = _date_part(_words(m.info.get("placeholder") or "")) or _date_part(_strong_text(m.info))
        if m.kind in ("birth_day", "birth_month", "birth_year"):
            part = m.kind[6:]
        explicit.append(part)
    if all(explicit) and len(set(explicit)) == len(explicit):
        return explicit  # type: ignore[return-value]
    lengths = [m.info.get("maxLength") or m.info.get("size") or 2 for m in members]
    if len(members) == 3:
        if lengths[0] == 4:
            return ["year", "month", "day"]
        lang = (members[0].lang or "").lower()
        if lang.startswith(_DMY_LANGS) and not lang.startswith("en-us"):
            return ["day", "month", "year"]
        return ["month", "day", "year"]
    return ["month", "year"] if lengths[-1] == 4 else ["month", "day"]


def _radio_group_kind(group: list[_Raw]) -> tuple[str | None, float]:
    """Kind of a radio group: from the group's label (legend / name / preceding text), else from its
    options - at least two male/female words ("Male"/"Female", "Mr"/"Mrs", "Herr"/"Frau"), so
    that a lone "Other" in a survey never counts as a gender question."""
    for r in group:
        if r.kind in ("gender", "card_type"):
            return r.kind, r.confidence
    labels = {_key(_radio_label(r.info)) for r in group}
    words = {_key(w) for w in _GENDER_WORDS["male"] + _GENDER_WORDS["female"] if len(w) > 1}
    if len(labels & words) >= 2:
        return "gender", 0.7
    brands = {_key(w) for ws in _CARD_BRANDS.values() for w in ws if len(w) > 2}
    if len(labels & brands) >= 2:
        return "card_type", 0.7
    return None, 0.0


def _resolve_names(raws: list[_Raw]) -> None:
    """Per form: a generic "Name" next to a first-name field is the last name (German "Vorname" +
    "Name", French "Prénom" + "Nom"); otherwise it is the full name."""
    forms: dict[tuple[int, int], list[_Raw]] = {}
    for r in raws:
        forms.setdefault((r.frame_index, r.info.get("form", -1)), []).append(r)
    for members in forms.values():
        kinds = {r.kind for r in members}
        for r in members:
            if r.kind == "name_generic":
                r.kind = "last_name" if "first_name" in kinds and "last_name" not in kinds else "full_name"


def _resolve_house_numbers(raws: list[_Raw]) -> None:
    """Per form: next to a separate house-number field, the street field gets the street name only."""
    forms: dict[tuple[int, int], list[_Raw]] = {}
    for r in raws:
        forms.setdefault((r.frame_index, r.info.get("form", -1)), []).append(r)
    for members in forms.values():
        if any(r.kind == "house_number" for r in members):
            for r in members:
                if r.kind == "street":
                    r.kind = "street_name"


# ---------------------------------------------------------------------- values

_HOUSE_NO = r"\d+\s*[a-z]?(?:\s*[-/]\s*\d+\s*[a-z]?)?"
_NUMBER_FIRST_RE = re.compile(rf"^({_HOUSE_NO})\s*,?\s+(\D.*)$", re.IGNORECASE)
_NUMBER_LAST_RE = re.compile(rf"^(.*\D)\s+({_HOUSE_NO})$", re.IGNORECASE)


def split_street(street: str | None) -> tuple[str, str] | None:
    """``(street name, house number)`` of an address line: ``"123 Test Street"`` (number first) or
    ``"Teststraße 12a"`` (number last); None when there is no house number to split off."""
    text = " ".join(str(street or "").split()).strip(" ,")
    if not text:
        return None
    match = _NUMBER_FIRST_RE.match(text) if text[0].isdigit() else None
    if match:
        return match.group(2).strip(" ,"), match.group(1).replace(" ", "")
    match = _NUMBER_LAST_RE.match(text)
    if match and match.group(1).strip(" ,"):
        return match.group(1).strip(" ,"), match.group(2).replace(" ", "")
    return None



def prepare_values(values: dict[str, str]) -> dict[str, str]:
    """``values`` plus derived ones (full name, DOB parts, card expiry, SSN parts, country name or
    code), without overriding anything the caller gave."""
    vals = {k: str(v) for k, v in (values or {}).items() if v is not None and str(v).strip()}
    for key, value in derived_values(vals).items():
        vals.setdefault(key, value)
    country = find_country(vals.get("country_code")) or find_country(vals.get("country"))
    if country is not None:
        vals.setdefault("country_code", country.code2)
        vals.setdefault("country", country.names[0])
    if vals.get("gender"):
        vals["gender"] = vals["gender"].strip().lower()
    parts = split_street(vals.get("street"))
    if parts is not None:
        vals.setdefault("street_name", parts[0])
        vals.setdefault("house_number", parts[1])
    return vals


def _digits(text: str | None) -> str:
    return re.sub(r"\D", "", text or "")


def _numeric_only(info: dict[str, Any]) -> bool:
    pattern = info.get("pattern") or ""
    return (info.get("inputmode") in ("numeric", "decimal") or info.get("type") == "number"
            or bool(re.fullmatch(r"\^?(\[0-9\]|\\d)(\{\d+(,\d*)?\}|[+*])\$?", pattern)))


def _format_by_pattern(digits: str, pattern: str) -> str | None:
    """``[0-9]{3}-[0-9]{3}-[0-9]{4}`` style patterns: lay ``digits`` out in their groups."""
    groups = re.findall(r"(?:\[0-9\]|\\d)\{(\d+)\}([^\[\\{]*)", pattern or "")
    if len(groups) < 2:
        return None
    total = sum(int(n) for n, _ in groups)
    if len(digits) < total:
        return None
    digits = digits[-total:]
    out, pos = [], 0
    for n, sep in groups:
        out.append(digits[pos:pos + int(n)] + sep.replace("\\", "").replace("$", ""))
        pos += int(n)
    return "".join(out)


def _phone_parts(vals: dict[str, str]) -> tuple[str, str]:
    """(country code without '+', national number as typed) from the phone value."""
    phone = (vals.get("phone") or "").strip()
    match = re.match(r"^\+(\d{1,3})[\s.\-/]+(.*)$", phone)
    if match:
        return match.group(1), match.group(2).strip()
    if phone.startswith("+"):
        return "", phone
    return "", phone


def _date_format(placeholder: str, lang: str) -> list[str]:
    """Order of day/month/year for a single text DOB field, plus separator (last element)."""
    ph = (placeholder or "").upper()
    tokens = re.findall(r"[A-Z]+", ph)
    sep_match = re.search(r"[A-Z]+([./\- ])[A-Z]+", ph)
    sep = sep_match.group(1) if sep_match else ("." if lang.startswith("de") else "/")
    if len(tokens) == 3:
        order = []
        for t in tokens:
            if t == "MM":
                order.append("month")
            elif len(t) == 4 or t in ("YY", "AA") or (t == "JJ" and "TT" in tokens):
                order.append("year4" if len(t) == 4 else "year2")
            else:
                order.append("day")
        if sorted(o[:4] for o in order) == ["day", "mont", "year"]:
            return order + [sep]
    if lang.startswith(_DMY_LANGS) and not lang.startswith("en-us"):
        return ["day", "month", "year4", sep]
    return ["month", "day", "year4", sep]


def text_value(kind: str, info: dict[str, Any], vals: dict[str, str]) -> str | None:
    """The text to enter into a text control of ``kind`` (format chosen from the control's
    maxlength / pattern / placeholder / inputmode), or None if there is no value."""
    max_len = info.get("maxLength")
    placeholder = (info.get("placeholder") or "").upper()
    lang = info.get("lang") or ""
    if kind in ("first_name", "middle_name", "last_name", "full_name", "email", "username", "password", "company",
                "city", "postal_code", "card_name", "card_cvv", "address_line2"):
        return vals.get(kind)
    if kind == "street_name":
        return vals.get("street_name") or vals.get("street")
    if kind == "street":
        street = vals.get("street")
        if street and info.get("tag") == "textarea" and vals.get("address_line2"):
            return f"{street}\n{vals['address_line2']}"
        return street
    if kind == "state":
        state = vals.get("state")
        if state and max_len == 2:
            region = find_region(state, vals.get("country_code"))
            return region[0] if region else state
        return state
    if kind == "country":
        country = find_country(vals.get("country_code")) or find_country(vals.get("country"))
        token = (info.get("autocomplete") or "").split()[-1:] or [""]
        if country is not None and (token[0] == "country" or (max_len and max_len <= 3)):
            return country.code3 if max_len == 3 else country.code2
        return vals.get("country") or (country.names[0] if country else None)
    if kind in ("phone", "phone_national", "phone_country_code", "phone_area", "phone_local", "phone_prefix",
                "phone_suffix"):
        return _phone_value(kind, info, vals)
    if kind == "birth_date":
        if not vals.get("birth_date"):
            return None
        y, m, d = vals["birth_date"].split("-")
        *order, sep = _date_format(info.get("placeholder") or "", lang)
        parts = {"day": d, "month": m, "year4": y, "year2": y[-2:]}
        return sep.join(parts[o] for o in order)
    if kind in ("birth_day", "birth_month"):
        return vals.get(kind)
    if kind == "birth_year":
        year = vals.get("birth_year")
        return year[-2:] if year and (max_len == 2 or re.search(r"\b(YY|JJ|AA)\b", placeholder)) else year
    if kind == "card_number":
        number = vals.get("card_number")
        return _digits(number) if number else None
    if kind == "card_exp":
        month, year = vals.get("card_exp_month"), vals.get("card_exp_year")
        if not (month and year):
            return None
        sep = " / " if " / " in placeholder else ("" if placeholder and "/" not in placeholder
                                                   and re.fullmatch(r"[MYJA]+", placeholder.replace(" ", "")) else "/")
        if re.search(r"(YYYY|JJJJ|AAAA)", placeholder):
            year_len = 4
        elif re.search(r"(YY|JJ|AA)", placeholder):
            year_len = 2
        else:
            year_len = 4 if max_len and max_len >= 2 + len(sep) + 4 else 2
        return f"{month}{sep}{year[-year_len:]}"
    if kind == "card_exp_month":
        return vals.get("card_exp_month")
    if kind == "card_exp_year":
        year = vals.get("card_exp_year")
        return year[-2:] if year and (max_len == 2 or re.search(r"\b(YY|JJ|AA)\b", placeholder)) else year
    if kind == "card_type":
        brand = vals.get("card_type")
        return {"amex": "American Express", "mastercard": "Mastercard"}.get(brand or "", (brand or "").capitalize()) or None
    if kind == "ssn":
        ssn = vals.get("ssn")
        if not ssn:
            return None
        digits = _digits(ssn)
        if max_len == 4:
            return digits[-4:]
        if max_len == 9 or _numeric_only(info):
            return digits
        return f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"
    if kind == "gender":
        gender = vals.get("gender")
        return gender.capitalize() if gender else None
    return vals.get(kind)


def _phone_value(kind: str, info: dict[str, Any], vals: dict[str, str]) -> str | None:
    phone = vals.get("phone")
    if not phone:
        return None
    digits = vals.get("phone_digits") or _digits(phone)
    country_code, national = _phone_parts(vals)
    national_digits = _digits(national) if country_code else digits
    if kind == "phone_country_code":
        return f"+{country_code}" if country_code else None
    if kind == "phone_area":
        return national_digits[-10:-7] or None
    if kind == "phone_prefix":
        return national_digits[-7:-4] or None
    if kind == "phone_suffix":
        return national_digits[-4:] or None
    if kind == "phone_local":
        return national_digits[-7:] or None
    max_len = info.get("maxLength")
    if kind == "phone_national":
        value = national if country_code else phone
        if max_len and len(value) > max_len:
            return national_digits[-max_len:]
        return value
    formatted = _format_by_pattern(digits, info.get("pattern") or "")
    if formatted:
        return formatted
    if max_len and len(phone) > max_len:
        return digits[-max_len:] if len(digits) > max_len else digits
    if _numeric_only(info):
        return digits[-max_len:] if max_len and len(digits) > max_len else digits
    return phone


def split_values(field_: DetectedField, vals: dict[str, str]) -> str | None:
    """The value for one part of a split field (phone 3-3-4, SSN 3-2-4, card 4x4)."""
    kind, index = field_.kind, field_.group_index or 0
    size = field_.group_size or 1
    if kind.startswith("birth_"):
        return text_value(kind, field_.info, vals)
    if kind == "phone":
        source = vals.get("phone_digits") or _digits(vals.get("phone"))
        family = "phone"
    elif kind == "ssn":
        source, family = _digits(vals.get("ssn")), "ssn"
    else:
        source, family = _digits(vals.get("card_number")), "card"
    if not source:
        return None
    lengths = list(field_.split_lengths) or [None] * size
    defaults = _DEFAULT_SPLITS.get(family, {}).get(size)
    if family == "card" and not defaults:
        defaults = [4] * (size - 1) + [max(len(source) - 4 * (size - 1), 1)]
        if size == 3 and len(source) == 15:
            defaults = [4, 6, 5]
    lengths = [n if n else (defaults[i] if defaults and i < len(defaults) else None) for i, n in enumerate(lengths)]
    if any(n is None for n in lengths):
        return None
    total = sum(lengths)  # type: ignore[arg-type]
    if family == "phone" and len(source) > total:
        source = source[-total:]  # drop the country code
    start = sum(lengths[:index])  # type: ignore[arg-type]
    if index == size - 1:
        return source[start:] or None
    return source[start:start + lengths[index]] or None  # type: ignore[operator]


def option_candidates(kind: str, vals: dict[str, str]) -> list[str]:
    """Option values/labels that would represent ``kind`` in a select, best first."""
    if kind == "country":
        country = find_country(vals.get("country_code")) or find_country(vals.get("country"))
        if country is None:
            return [vals["country"]] if vals.get("country") else []
        return [country.code2, country.code3, *country.names, country.numeric]
    if kind == "state":
        state = vals.get("state")
        if not state:
            return []
        region = find_region(state, vals.get("country_code"))
        return [region[0], region[1], state] if region else [state]
    if kind in ("birth_month", "card_exp_month"):
        value = vals.get(kind)
        if not value or not value.isdigit() or not 1 <= int(value) <= 12:
            return []
        m = int(value)
        return [f"{m:02d}", str(m), *_MONTHS[m - 1]]
    if kind in ("birth_year", "card_exp_year"):
        year = vals.get(kind)
        return [year, year[-2:]] if year else []
    if kind == "birth_day":
        day = vals.get(kind)
        return [f"{int(day):02d}", str(int(day))] if day and day.isdigit() else []
    if kind == "gender":
        gender = vals.get("gender")
        return list(_GENDER_WORDS.get(gender or "", ())) if gender else []
    if kind == "card_type":
        brand = vals.get("card_type")
        return list(_CARD_BRANDS.get(brand or "", (brand,))) if brand else []
    if kind == "card_exp":
        month, year = vals.get("card_exp_month"), vals.get("card_exp_year")
        return [f"{month}/{year[-2:]}", f"{month}/{year}", f"{month}{year[-2:]}"] if month and year else []
    if kind == "phone_country_code":
        code, _ = _phone_parts(vals)
        return [f"+{code}", code] if code else []
    value = text_value(kind, {}, vals)
    return [value] if value else []


_NUMERIC_KINDS = frozenset({"birth_day", "birth_month", "birth_year", "card_exp_month", "card_exp_year"})


def match_option(kind: str, vals: dict[str, str], options: list[list[Any]] | list[tuple[Any, ...]]) -> int | None:
    """Index of the option of a ``<select>`` (``[[value, label, disabled], ...]``) that represents
    ``kind`` for these values, or None. Case/accent-insensitive; numbers compare numerically."""
    candidates = [c for c in option_candidates(kind, vals) if c]
    if not candidates:
        return None
    real = set(_real_options(options))
    usable = [(i, str(o[0]), str(o[1])) for i, o in enumerate(options) if (str(o[0]), str(o[1])) in real]
    keys = [_key(c) for c in candidates]
    # 1. exact value / label (normalised), in candidate order
    for k in keys:
        if not k:
            continue
        for i, v, t in usable:
            if _key(v) == k or _key(t) == k:
                return i
    # 2. numbers: "04 - April", "4", "2031"
    if kind in _NUMERIC_KINDS:
        number = int(_digits(candidates[0]) or 0)
        short = number % 100 if kind.endswith("year") else None
        for i, v, t in usable:
            for text in (v, t):
                m = re.match(r"^\s*(\d{1,4})\b", text)
                if m and (int(m.group(1)) == number or (short is not None and len(m.group(1)) == 2
                                                         and int(m.group(1)) == short)):
                    return i
        month_names = {_key(n) for n in candidates[2:]} if kind.endswith("month") else set()
        for i, v, t in usable:
            if month_names & {_key(w) for w in re.split(r"[\s\-./()]+", _fold(t)) if w}:
                return i
        return None
    # 3. whole-word containment ("CA - California", "United States (+1)"); never "male" in "female"
    for k, c in zip(keys, candidates):
        if len(k) < 2:
            continue
        words = _fold(c).split()
        pattern = re.compile(r"(?<![0-9a-z])" + r"\W*".join(re.escape(_key(w)) for w in words if _key(w)) + r"(?![0-9a-z])")
        for i, v, t in usable:
            if kind == "phone_country_code":
                if re.search(r"(?<![\d])\+?" + re.escape(_digits(c)) + r"(?![\d])", f"{v} {t}"):
                    return i
                continue
            if pattern.search(_fold(t)) or pattern.search(_fold(v)):
                return i
    return None


# ---------------------------------------------------------------------- fill


def _is_sensitive(kind: str, sensitive_keys: set[str]) -> bool:
    return kind not in NOT_SECRET_KINDS and bool(KIND_SOURCES.get(kind, frozenset({kind})) & sensitive_keys)


def _requested(kind: str, only: set[str] | None) -> bool:
    return only is None or kind in only or bool(KIND_SOURCES.get(kind, frozenset()) & only)


_SECRET_DERIVED = frozenset({"ssn_digits", "ssn_area", "ssn_group", "ssn_serial", "card_exp", "card_exp_full"})


class _Scrubber:
    """Removes sensitive values (and their digit-only forms) from report text. Descriptors and
    errors are page/driver text, never values: this is defence in depth. Non-sensitive values are
    shown to the model by identity_show anyway, and scrubbing short words such as "male" would
    mangle labels."""

    def __init__(self, vals: dict[str, str], sensitive: set[str]) -> None:
        needles: set[str] = set()
        for key, value in vals.items():
            if key not in sensitive and key not in _SECRET_DERIVED:
                continue
            value = str(value).strip()
            for needle in (value, _digits(value)):
                if len(needle) >= 3:
                    needles.add(needle)
        self.needles = sorted(needles, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for needle in self.needles:
            if needle in text:
                text = text.replace(needle, "•••")
        return text


def _error_reason(exc: BaseException, scrub: _Scrubber) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    text = re.sub(r"^\w+\.\w+: ", "", text)  # "Locator.fill: ..." -> "..."
    return "error: " + scrub(text)[:160]


FrameOriginPolicy = Callable[[str, str], bool]
"""``(field kind, frame origin) -> allowed``: may a sensitive value of that kind go into a child
frame of that origin? See :func:`autofill` (``sensitive_frame_origins``)."""
ProgressCallback = Callable[[int, int], Awaitable[None]]
"""``(fields processed so far, fields known so far)``, awaited after every field."""

NOT_VISIBLE = "not visible (covered or clipped)"
NOT_STORED = "sensitive value not stored in the identity"
COUNTRY_MISMATCH = ("kept the form's preselected country, which differs from the identity's; replace it with "
                    "fields=['country', 'state'] and overwrite=true")
THIRD_PARTY_FRAME = "sensitive field in a third-party frame"


class _NotReachable(Exception):
    """The field (or the radio / label to click) is covered or clipped where a person would click."""


@dataclass
class _Ctx:
    page: "Page"
    vals: dict[str, str]
    method: TypeMethod
    sensitive_keys: set[str]
    clipboard_lock: Path
    rng: random.Random
    wpm: int | None
    scrub: _Scrubber
    frame_policy: FrameOriginPolicy | None = None
    secrets_given: bool = False
    """Did the caller pass any sensitive value (the sensitive tool) or none (form_autofill)?"""
    progress: ProgressCallback | None = None
    done: int = 0


async def autofill(
    page: "Page",
    values: dict[str, str],
    *,
    method: TypeMethod = "paste",
    sensitive_keys: set[str] | frozenset[str],
    clipboard_lock: Path,
    only: set[str] | None = None,
    overwrite: bool = False,
    rng: random.Random | None = None,
    scope: "Locator | None" = None,
    wpm: int | None = None,
    pause: tuple[float, float] = FIELD_PAUSE,
    sensitive_frame_origins: FrameOriginPolicy | None = None,
    progress: ProgressCallback | None = None,
) -> AutofillReport:
    """Detect the form fields of ``page`` (or inside ``scope``) and fill them from ``values``
    (identity keys -> values, e.g. :meth:`IdentityStore.fill_values`).

    ``method`` is the text entry method (:func:`enter_text`); ``sensitive_keys`` (always joined with
    the identity registry's sensitive fields) mark values whose clipboard content is concealed;
    ``only`` limits the identity keys used; ``overwrite`` also replaces values already in the form.
    Fields that appear after the first pass (a state select after the country) get a second pass.

    Every field is checked right before it is filled: a field a person could not see or reach
    (covered by an overlay, clipped) is skipped. A sensitive value goes into a child frame only
    when ``sensitive_frame_origins(kind, origin)`` allows that frame's *current* origin and the
    origin of every frame around it (fail closed: without a policy, sensitive values never go into
    any child frame). ``progress`` is awaited after every field.
    """
    from .driver import Error as PlaywrightError

    rng = rng or random.Random()
    vals = prepare_values(values)
    sens = set(sensitive_keys) | set(SENSITIVE_FIELDS)
    ctx = _Ctx(page, vals, method, sens, Path(clipboard_lock), rng, wpm, _Scrubber(vals, sens),
               frame_policy=sensitive_frame_origins, secrets_given=any(k in sens for k in vals),
               progress=progress)
    report = AutofillReport()
    seen: set[tuple[Any, ...]] = set()
    paused = [False]
    only = set(only) if only is not None else None
    for pass_no in (1, 2):
        last = pass_no == 2
        fields = await detect_fields(page, scope=scope)
        stale = False
        try:
            todo = []
            counts: dict[tuple[Any, ...], int] = {}
            for f in fields:
                base = (f.frame_url, f.kind, f.descriptor, f.group_index)
                counts[base] = counts.get(base, 0) + 1
                key = (*base, counts[base])
                if key in seen or (last and f.has_value):
                    continue
                seen.add(key)
                todo.append((key, f))
            filled_before = len(report.filled)
            deferred: list[tuple[tuple[Any, ...], DetectedField]] = []
            for position, (key, f) in enumerate(todo, 1):
                retry = await _process(ctx, f, report, overwrite=overwrite, only=only, paused=paused, pause=pause,
                                       last_pass=last)
                if retry == "defer":
                    deferred.append((key, f))
                elif retry == "again":
                    seen.discard(key)
                    stale = True
                if retry != "defer":
                    await _progress(ctx, len(todo) - position + len(deferred))
            for remaining, (key, f) in enumerate(reversed(deferred)):
                try:
                    f.info["options"] = await _wait_for_options(ctx, f)
                except PlaywrightError:
                    if last:
                        report.skipped.append({**_entry(ctx, f), "reason": _CHANGED})
                    else:
                        seen.discard(key)
                        stale = True
                    await _progress(ctx, remaining)
                    continue
                retry = await _process(ctx, f, report, overwrite=overwrite, only=only, paused=paused, pause=pause,
                                       final=True, last_pass=last)
                if retry == "again":
                    seen.discard(key)
                    stale = True
                await _progress(ctx, remaining)
        finally:
            await dispose_fields(fields)
        if not last and len(report.filled) == filled_before and not stale:
            break  # nothing changed on the page: no second pass needed
    return report


async def _progress(ctx: _Ctx, remaining: int) -> None:
    ctx.done += 1
    if ctx.progress is not None:
        try:
            await ctx.progress(ctx.done, ctx.done + remaining)
        except Exception as exc:  # progress is cosmetic: never let it break a fill
            log.debug("autofill progress callback failed (%s)", type(exc).__name__)


_CHANGED = "the form changed while it was being filled; run autofill again"


def _entry(ctx: _Ctx, f: DetectedField) -> dict[str, Any]:
    entry: dict[str, Any] = {"kind": f.kind, "field": ctx.scrub(f.descriptor)}
    if f.frame is not None and f.frame.parent_frame is not None:
        entry["frame"] = _origin(f.frame_url)
    if f.group_index is not None and f.group_size:
        entry["part"] = f"{f.group_index + 1}/{f.group_size}"
    return entry


async def _wait_for_options(ctx: _Ctx, f: DetectedField) -> list[list[Any]]:
    deadline = asyncio.get_running_loop().time() + OPTION_WAIT_SECONDS
    while True:
        options = await f.element.evaluate(_OPTIONS_JS)
        if match_option(f.kind, ctx.vals, options) is not None or asyncio.get_running_loop().time() >= deadline:
            return options
        await asyncio.sleep(0.1)


def _kept_reason(ctx: _Ctx, f: DetectedField) -> str:
    """Why a field with a value was left alone. A page's preselected country (Shopify-style
    checkouts guess it from the IP) that differs from the identity's makes the address inconsistent:
    that gets a reason the caller can act on."""
    if f.control == "select" and f.kind == "country":
        index = match_option(f.kind, ctx.vals, f.info.get("options") or [])
        if index is not None and index != f.info.get("selectedIndex"):
            return COUNTRY_MISMATCH
    return "already has a value"


async def _process(ctx: _Ctx, f: DetectedField, report: AutofillReport, *, overwrite: bool, only: set[str] | None,
                   paused: list[bool], pause: tuple[float, float], final: bool = False,
                   last_pass: bool = False) -> str | None:
    """Fill one field and record it. Returns "defer" (select without a matching option yet),
    "again" (element went stale before the last pass: the next pass finds it again) or None."""
    from .driver import Error as PlaywrightError

    if not _requested(f.kind, only):
        return None
    entry = _entry(ctx, f)
    if f.has_value and not overwrite:
        report.skipped.append({**entry, "reason": _kept_reason(ctx, f)})
        return None
    sensitive = _is_sensitive(f.kind, ctx.sensitive_keys)
    value: str | None = None
    if f.control == "select":
        options = f.info.get("options") or []
        index = match_option(f.kind, ctx.vals, options)
        if index is None:
            if not option_candidates(f.kind, ctx.vals):
                report.skipped.append({**entry, "reason": _missing_reason(f.kind, ctx)})
                return None
            if not final:
                return "defer"
            report.skipped.append({**entry, "reason": "no matching option"})
            return None
    elif f.control == "radio":
        index = None
        if not option_candidates(f.kind, ctx.vals):
            report.skipped.append({**entry, "reason": _missing_reason(f.kind, ctx)})
            return None
    else:
        value = split_values(f, ctx.vals) if f.group_index is not None else _value_for_text(f, ctx.vals)
        if not value:
            report.skipped.append({**entry, "reason": _missing_reason(f.kind, ctx)})
            return None
    try:
        if sensitive and f.frame is not None and f.frame.parent_frame is not None:
            refusal = await _frame_refusal(ctx, f)  # the frames' origins *now*, right before the value goes in
            if refusal:
                report.skipped.append({**entry, "reason": refusal})
                return None
        if paused[0] and ctx.method in ("human", "paste") and pause[1] > 0:
            await asyncio.sleep(ctx.rng.uniform(*pause))
        paused[0] = True
        if f.control != "radio":
            await _ensure_reachable(f.element, f.frame)
        if f.control == "select":
            await f.element.select_option(index=index, timeout=ACTION_TIMEOUT_MS)
            used = "select"
        elif f.control == "radio":
            used = await _choose_radio(ctx, f)
            if used is None:
                report.skipped.append({**entry, "reason": "no matching option"})
                return None
        elif f.control == "date":
            assert value is not None
            await f.element.fill(value, timeout=ACTION_TIMEOUT_MS)
            used = "fill"
        else:
            assert value is not None
            if sensitive:
                report.secret_texts.append(value)
            used = await enter_text(ctx.page, f.element, value, method=ctx.method, clear=True, sensitive=sensitive,
                                    clipboard_lock=ctx.clipboard_lock, rng=ctx.rng, wpm=ctx.wpm)
    except _NotReachable:
        report.skipped.append({**entry, "reason": NOT_VISIBLE})
        return None
    except NotTypeableError as exc:
        report.skipped.append({**entry, "reason": ctx.scrub(str(exc))})
        return None
    except (*PlaywrightError, TextEntryError) as exc:  # PlaywrightError is a tuple (one class per driver)
        if _stale(exc):
            if not last_pass:
                return "again"
            report.skipped.append({**entry, "reason": _CHANGED})
            return None
        report.skipped.append({**entry, "reason": _error_reason(exc, ctx.scrub)})
        return None
    report.filled.append({**entry, "method": used})
    log.debug("autofill: %s filled via %s", f.kind, used)
    return None


async def _frame_origin(frame: "Frame") -> str:
    """The frame's current origin. ``frame.url`` comes from the browser (a page script cannot fake
    it); ``about:srcdoc`` / ``about:blank`` / ``blob:`` frames, and out-of-process frames that a late
    connection has not seen navigate yet (url ''), report the origin of their document."""
    from .driver import Error as PlaywrightError

    url = frame.url or ""
    if re.match(r"^https?://", url, re.IGNORECASE):
        return _origin(url)
    try:
        origin = await frame.evaluate("() => window.location.origin")
    except PlaywrightError:
        return "null"
    return origin if isinstance(origin, str) and origin else "null"


async def _frame_refusal(ctx: _Ctx, f: DetectedField) -> str | None:
    """None when a sensitive value of ``f.kind`` may go into ``f``'s frame and every frame around it
    (so a payment iframe nested inside an untrusted frame is refused too), else the skip reason."""
    frame = f.frame
    while frame is not None and frame.parent_frame is not None:
        if frame.is_detached():
            return _CHANGED
        origin = await _frame_origin(frame)
        if ctx.frame_policy is None or not ctx.frame_policy(f.kind, origin):
            return f"{THIRD_PARTY_FRAME} ({origin})"
        frame = frame.parent_frame
    return None


async def _ensure_reachable(element: "ElementHandle", frame: "Frame | None") -> None:
    """Scroll ``element`` into view and raise :class:`_NotReachable` unless a click at one of a few
    points of it would reach it (or its label) - in its own document and, for fields in iframes,
    through every ``<iframe>`` element up to the page."""
    point = await element.evaluate(_HIT_JS)
    child = frame
    while point and child is not None and child.parent_frame is not None:
        frame_element = await child.frame_element()
        try:
            point = await frame_element.evaluate(_FRAME_HIT_JS, point)
        finally:
            await frame_element.dispose()
        child = child.parent_frame
    if not point:
        raise _NotReachable()


def _stale(exc: BaseException) -> bool:
    text = str(exc)
    return any(s in text for s in ("not attached", "detached", "Execution context was destroyed",
                                   "Element is not attached", "Frame was detached"))


def _missing_reason(kind: str, ctx: _Ctx) -> str:
    sources = KIND_SOURCES.get(kind, frozenset({kind}))
    if kind == "card_type" and "card_number" not in ctx.vals:
        return ("no card number stored in the identity" if ctx.secrets_given else
                "filled together with the card number by form_autofill_sensitive")
    if kind == "house_number" and ctx.vals.get("street"):
        return "the identity's street has no house number to split off (it went into the street field)"
    if sources & SENSITIVE_FIELDS and not any(k in ctx.vals for k in sources):
        return NOT_STORED if ctx.secrets_given else "sensitive field (not included in this fill)"
    return "no value in the identity"


def _value_for_text(f: DetectedField, vals: dict[str, str]) -> str | None:
    if f.control == "date":
        typ = f.info.get("type")
        if f.kind == "birth_date" and typ == "date":
            return vals.get("birth_date")
        if f.kind == "card_exp" and typ == "month" and vals.get("card_exp_year") and vals.get("card_exp_month"):
            return f"{vals['card_exp_year']}-{vals['card_exp_month']}"
        return None
    return text_value(f.kind, f.info, vals)


async def _choose_radio(ctx: _Ctx, f: DetectedField) -> str | None:
    options = [[m_info.get("choiceValue") or "", _radio_label(m_info), False] for _, m_info in f.members]
    index = match_option(f.kind, ctx.vals, options)
    if index is None:
        return None
    element, info = f.members[index]
    if info.get("inputVisible"):
        await _ensure_reachable(element, f.frame)
        await element.click(timeout=ACTION_TIMEOUT_MS)
    else:  # custom-styled radio: the native input is hidden, its label is what people click
        label = (await element.evaluate_handle(
            "e => [...(e.labels || [])].find(l => { const r = l.getBoundingClientRect(); "
            "return r.width > 1 && r.height > 1; }) || null")).as_element()
        if label is None:
            raise _NotReachable()
        try:
            await _ensure_reachable(label, f.frame)
            await label.click(timeout=ACTION_TIMEOUT_MS)
        finally:
            await label.dispose()
    if not await element.evaluate("e => e.checked"):
        await element.check(timeout=ACTION_TIMEOUT_MS, force=True)
    return "click"


def known_kinds() -> list[str]:
    """Every field kind :func:`detect_fields` can report."""
    return sorted(KIND_SOURCES)
