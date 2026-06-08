with open('/home/trader/apps/trading-bot/app/static/dashboard.js', encoding='utf-8') as f:
    content = f.read()
o = content.count('{') - content.count('}')
p = content.count('(') - content.count(')')
b = content.count('[') - content.count(']')
bt = content.count('`')
print(f'Brace balance: {o}')
print(f'Paren balance: {p}')
print(f'Bracket balance: {b}')
print(f'Backticks count: {bt}, even={bt%2==0}')

# Find syntax issues - look for the comment we added
lines = content.split('\n')
for i, line in enumerate(lines, 1):
    if '\\' in line and 'data-sig' in line:
        print(f'Line {i}: {repr(line[:100])}')
