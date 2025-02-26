import re
def smart_split(options):
    if re.search(r',? ?\bor\b ?|, ?',options.lower()) is not None:
        values = re.split(r',? ?\bor\b ?|, ?',options.lower())
    elif ' ' in options:
        values = options.split(' ')
    else:
        values = re.split(r'\W',options)
    return [value for value in values if value != ""]