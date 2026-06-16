"""Quick IB Gateway paper connectivity test."""
from ib_insync import IB

ib = IB()
ib.connect('127.0.0.1', 4001, clientId=11, timeout=30)
print('connected:', ib.isConnected())
print('server version:', ib.client.serverVersion())
accounts = ib.managedAccounts()
print('managed accounts:', accounts)
for acct in accounts:
    summ = ib.accountSummary(acct)
    vals = {r.tag: r.value for r in summ if r.tag in
            ('AccountType', 'NetLiquidation', 'TotalCashValue',
             'AvailableFunds', 'BuyingPower', 'Currency')}
    print(f'--- {acct} ---')
    for k, v in vals.items():
        print(f'  {k}: {v}')
ib.disconnect()
print('disconnected')
