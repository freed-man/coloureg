"""
UK Vehicle tax rates based on GOV.UK data.
Rates effective from April 2025.
https://www.gov.uk/vehicle-tax-rate-tables
"""

# Post April 2017 - first year rates by CO2 max threshold
# (petrol, RDE2 diesel, alternative fuel)
FIRST_YEAR_STANDARD = [
    (0, 10), (50, 110), (75, 130), (90, 270), (100, 350),
    (110, 390), (130, 440), (150, 540), (170, 1360),
    (190, 2190), (225, 3300), (255, 4680),
]
FIRST_YEAR_STANDARD_OVER_255 = 5490

# Post April 2017 - first year rates (non-RDE2 diesel)
FIRST_YEAR_DIESEL = [
    (0, 10), (50, 130), (75, 270), (90, 350), (100, 390),
    (110, 440), (130, 540), (150, 1360), (170, 2190),
    (190, 3300), (225, 4680), (255, 5490),
]
FIRST_YEAR_DIESEL_OVER_255 = 5490

# Post April 2017 - standard rate (second year onwards)
STANDARD_RATE_12 = 195
STANDARD_RATE_6 = 107.25

# Expensive car supplement (list price over £40,000)
EXPENSIVE_SUPPLEMENT = 425

# 2001-2017 rates by CO2 band (threshold, 12 month, 6 month)
RATES_2001_2017 = [
    (100, 20, None),        # A: up to 100
    (110, 20, None),        # B: 101-110
    (120, 35, None),        # C: 111-120
    (130, 165, 90.75),      # D: 121-130
    (140, 195, 107.25),     # E: 131-140
    (150, 215, 118.25),     # F: 141-150
    (165, 265, 145.75),     # G: 151-165
    (175, 315, 173.25),     # H: 166-175
    (185, 345, 189.75),     # I: 176-185
    (200, 395, 217.25),     # J: 186-200
    (225, 430, 236.50),     # K: 201-225
    (255, 735, 404.25),     # L: 226-255
]
RATES_2001_2017_OVER_255 = (760, 418)  # M: over 255

# Pre-2001 rates by engine size
PRE_2001_SMALL = 230      # up to 1549cc
PRE_2001_SMALL_6M = 126.50
PRE_2001_LARGE = 375      # over 1549cc
PRE_2001_LARGE_6M = 206.25


def get_annual_tax(co2, fuel_type, reg_year, reg_month, engine_cc=None):
    """
    Calculate estimated annual vehicle tax.
    Returns dict with rate info.
    """
    result = {
        'annual_rate': None,
        'six_month_rate': None,
        'first_year_rate': None,
        'band': '',
        'note': '',
    }

    if reg_year is None:
        result['note'] = 'Registration date unknown'
        return result

    # post April 2017
    if reg_year > 2017 or (reg_year == 2017 and reg_month and reg_month >= 4):
        result['annual_rate'] = STANDARD_RATE_12
        result['six_month_rate'] = STANDARD_RATE_6
        result['note'] = 'Standard rate'

        if co2 is not None:
            if fuel_type == 'DIESEL':
                rates = FIRST_YEAR_DIESEL
                over = FIRST_YEAR_DIESEL_OVER_255
            else:
                rates = FIRST_YEAR_STANDARD
                over = FIRST_YEAR_STANDARD_OVER_255

            first_year = over
            for threshold, rate in rates:
                if co2 <= threshold:
                    first_year = rate
                    break
            result['first_year_rate'] = first_year

        return result

    # 2001-2017
    if reg_year >= 2001:
        if co2 is not None:
            bands = 'ABCDEFGHIJKLM'
            found = False
            for i, (threshold, annual, six_month) in enumerate(RATES_2001_2017):
                if co2 <= threshold:
                    result['annual_rate'] = annual
                    result['six_month_rate'] = six_month
                    result['band'] = bands[i]
                    found = True
                    break
            if not found:
                result['annual_rate'] = RATES_2001_2017_OVER_255[0]
                result['six_month_rate'] = RATES_2001_2017_OVER_255[1]
                result['band'] = 'M'
        result['note'] = 'Based on CO2 emissions'
        return result

    # pre-2001
    if engine_cc is not None:
        if engine_cc <= 1549:
            result['annual_rate'] = PRE_2001_SMALL
            result['six_month_rate'] = PRE_2001_SMALL_6M
        else:
            result['annual_rate'] = PRE_2001_LARGE
            result['six_month_rate'] = PRE_2001_LARGE_6M
        result['note'] = 'Based on engine size'
    return result