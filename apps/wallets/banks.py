"""The banks a member's wallet can be paid into.

Commercial and non-interest banks licensed by the CBN, and the payment
service and microfinance banks members use most. A bank is named by a slug:
the bank API, once connected, maps it to its own code and supplies the full
list.
"""

BANKS = {
    "access": "Access Bank",
    "citibank": "Citibank Nigeria",
    "ecobank": "Ecobank Nigeria",
    "fidelity": "Fidelity Bank",
    "first-bank": "First Bank of Nigeria",
    "fcmb": "First City Monument Bank (FCMB)",
    "globus": "Globus Bank",
    "gtbank": "Guaranty Trust Bank (GTBank)",
    "jaiz": "Jaiz Bank",
    "keystone": "Keystone Bank",
    "lotus": "Lotus Bank",
    "optimus": "Optimus Bank",
    "parallex": "Parallex Bank",
    "polaris": "Polaris Bank",
    "premiumtrust": "PremiumTrust Bank",
    "providus": "Providus Bank",
    "signature": "Signature Bank",
    "stanbic-ibtc": "Stanbic IBTC Bank",
    "standard-chartered": "Standard Chartered Bank",
    "sterling": "Sterling Bank",
    "suntrust": "SunTrust Bank",
    "taj": "TAJBank",
    "titan-trust": "Titan Trust Bank",
    "union": "Union Bank of Nigeria",
    "uba": "United Bank for Africa (UBA)",
    "unity": "Unity Bank",
    "wema": "Wema Bank (ALAT)",
    "zenith": "Zenith Bank",
    "opay": "OPay",
    "palmpay": "PalmPay",
    "moniepoint": "Moniepoint Microfinance Bank",
    "kuda": "Kuda Microfinance Bank",
    "fairmoney": "FairMoney Microfinance Bank",
    "carbon": "Carbon Microfinance Bank",
    "vfd": "VFD Microfinance Bank",
    "momo-psb": "MoMo Payment Service Bank",
    "9psb": "9 Payment Service Bank",
    "smartcash-psb": "SmartCash Payment Service Bank",
    "hope-psb": "HopePSB",
    "lapo": "LAPO Microfinance Bank",
    "ab-mfb": "AB Microfinance Bank",
    "accion": "Accion Microfinance Bank",
}

BANK_CHOICES = sorted(BANKS.items(), key=lambda bank: bank[1].casefold())
