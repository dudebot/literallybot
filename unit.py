import re
["option1, option2","option 1, option 2","option 1, or option 2","option 1, option 2, or option 3","option 1, option 2 or option 3","option 1 or option 2 or option 3"]
tests = ["option1, option2","option 1, option 2","option 1, or option 2","option 1, option 2, or option 3","option 1, option 2 or option 3","option 1 or option 2 or option 3"]
for i in tests:
  match = re.split(r'(,? ?(\bor\b) ?)|(, ?(\bor\b)? ?)',i,)
  match = [j for j in match if j is not None and re.match(r'(,? ?(\bor\b) ?)|(, ?(\bor\b)? ?)',j) is None]
  print(i)
  if match is not None:
    print(match)
  else:
    print("no match")

