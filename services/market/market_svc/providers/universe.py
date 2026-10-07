"""Seed universe: indices + liquid NSE names + popular MF schemes. Used to seed the security
master and to drive the offline simulator with realistic starting prices/sectors."""
from __future__ import annotations

INDICES = [
    ("^NSEI", "NIFTY 50", 25000.0),
    ("^BSESN", "S&P BSE SENSEX", 82000.0),
    ("^NSEBANK", "NIFTY BANK", 54000.0),
    ("^CNXIT", "NIFTY IT", 36000.0),
    ("^CNXPHARMA", "NIFTY PHARMA", 22000.0),
    ("^CNXAUTO", "NIFTY AUTO", 24000.0),
    ("^CNXFMCG", "NIFTY FMCG", 56000.0),
    ("^CNXMETAL", "NIFTY METAL", 9200.0),
    ("^CRSLDX", "NIFTY 500", 23000.0),
    ("^INDIAVIX", "INDIA VIX", 13.5),
]

SECTOR_INDEX = {
    "Financial Services": "^NSEBANK",
    "Information Technology": "^CNXIT",
    "Healthcare": "^CNXPHARMA",
    "Automobile": "^CNXAUTO",
    "FMCG": "^CNXFMCG",
    "Metals & Mining": "^CNXMETAL",
}

# symbol, name, sector, base price, annual drift, annual vol, quality (0..1 drives simulated fundamentals)
STOCKS = [
    ("RELIANCE.NS", "Reliance Industries", "Oil Gas & Energy", 2950.0, 0.12, 0.24, 0.70),
    ("TCS.NS", "Tata Consultancy Services", "Information Technology", 4100.0, 0.10, 0.20, 0.92),
    ("HDFCBANK.NS", "HDFC Bank", "Financial Services", 1650.0, 0.11, 0.21, 0.85),
    ("INFY.NS", "Infosys", "Information Technology", 1850.0, 0.10, 0.23, 0.88),
    ("ICICIBANK.NS", "ICICI Bank", "Financial Services", 1250.0, 0.16, 0.22, 0.84),
    ("HINDUNILVR.NS", "Hindustan Unilever", "FMCG", 2600.0, 0.07, 0.18, 0.86),
    ("ITC.NS", "ITC", "FMCG", 480.0, 0.10, 0.19, 0.80),
    ("SBIN.NS", "State Bank of India", "Financial Services", 810.0, 0.14, 0.28, 0.62),
    ("BHARTIARTL.NS", "Bharti Airtel", "Telecommunication", 1600.0, 0.18, 0.22, 0.66),
    ("LT.NS", "Larsen & Toubro", "Construction", 3600.0, 0.14, 0.24, 0.74),
    ("BAJFINANCE.NS", "Bajaj Finance", "Financial Services", 7200.0, 0.13, 0.30, 0.78),
    ("MARUTI.NS", "Maruti Suzuki", "Automobile", 12500.0, 0.11, 0.23, 0.79),
    ("TMPV.NS", "Tata Motors Passenger Vehicles", "Automobile", 950.0, 0.02, 0.38, 0.48),
    ("SUNPHARMA.NS", "Sun Pharmaceutical", "Healthcare", 1750.0, 0.15, 0.22, 0.77),
    ("TITAN.NS", "Titan Company", "Consumer Durables", 3500.0, 0.12, 0.25, 0.82),
    ("ASIANPAINT.NS", "Asian Paints", "Consumer Durables", 2900.0, -0.05, 0.22, 0.83),
    ("WIPRO.NS", "Wipro", "Information Technology", 540.0, 0.04, 0.26, 0.64),
    ("TATASTEEL.NS", "Tata Steel", "Metals & Mining", 155.0, 0.03, 0.34, 0.45),
    ("ADANIENT.NS", "Adani Enterprises", "Metals & Mining", 3000.0, -0.08, 0.45, 0.35),
    ("YESBANK.NS", "Yes Bank", "Financial Services", 22.0, -0.10, 0.42, 0.20),
    ("ETERNAL.NS", "Eternal (Zomato)", "Consumer Services", 250.0, 0.25, 0.45, 0.50),
    ("IRCTC.NS", "IRCTC", "Consumer Services", 880.0, 0.05, 0.30, 0.72),
    ("NIFTYBEES.NS", "Nippon India ETF Nifty 50 BeES", "ETF", 275.0, 0.11, 0.16, 0.80),
    ("GOLDBEES.NS", "Nippon India ETF Gold BeES", "ETF", 65.0, 0.10, 0.14, 0.80),
]

# AMFI scheme code, name, category, plan, expense ratio, drift, vol
MUTUAL_FUNDS = [
    ("122639", "Parag Parikh Flexi Cap Fund - Direct Growth", "equity:flexi_cap", "direct", 0.63, 0.17, 0.15),
    ("120503", "Axis ELSS Tax Saver Fund - Direct Growth", "equity:elss", "direct", 0.80, 0.08, 0.17),
    ("118989", "HDFC Mid-Cap Opportunities Fund - Direct Growth", "equity:mid_cap", "direct", 0.77, 0.20, 0.19),
    ("120716", "UTI Nifty 50 Index Fund - Direct Growth", "equity:index", "direct", 0.18, 0.12, 0.16),
    ("125497", "SBI Small Cap Fund - Direct Growth", "equity:small_cap", "direct", 0.68, 0.18, 0.23),
    ("100122", "HDFC Mid-Cap Opportunities Fund - Regular Growth", "equity:mid_cap", "regular", 1.45, 0.19, 0.19),
    ("119062", "HDFC Corporate Bond Fund - Direct Growth", "debt:corporate_bond", "direct", 0.36, 0.075, 0.02),
    ("118825", "Mirae Asset Large Cap Fund - Direct Growth", "equity:large_cap", "direct", 0.55, 0.11, 0.15),
]

# NPS schemes (PFM, tier, asset class) — NAVs are manual/simulated in P1
NPS_SCHEMES = [
    ("NPS-SBI-E-I", "SBI Pension Fund Scheme E - Tier I", "E", 0.13, 0.16),
    ("NPS-SBI-C-I", "SBI Pension Fund Scheme C - Tier I", "C", 0.08, 0.03),
    ("NPS-SBI-G-I", "SBI Pension Fund Scheme G - Tier I", "G", 0.075, 0.04),
    ("NPS-HDFC-E-I", "HDFC Pension Fund Scheme E - Tier I", "E", 0.14, 0.16),
]
