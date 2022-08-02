import requests
import bs4

import re
import pandas as pd

from fuzzywuzzy import process

'''
todo 
this needs to be a class since df does not self update at the end of update_wf_data()
???
'''


matchdata_re = re.compile(r"([^/]+)/([^/]+) \((.+)\)")
item_re = re.compile(r"((\d{1,3},?)*) ?(.+)")
chance_re = re.compile(r".*\((.*)\%\)")

def parse_matchdata(matchdata):
    match = matchdata_re.match(matchdata)
    if match is None:
        return None #return list of empty?
    else:
         return match.groups()
def parse_item(item_str):
    match = item_re.match(item_str)
    if match is None:
        return None #return list of empty?
    else:
        num = match.groups()[0].replace(",","")
        try:
            num = int(num)
        except:
            num = 1
        item = match.groups()[2]
        return [num,item]
def parse_chance(chance_str):
    match = chance_re.match(chance_str)
    return float(match.groups()[0])/100

def is_endless_mission(missiontype):
    if missiontype in ['Survival','Defense','Interception','Excavation','Defection','Infested Salvage','Sanctuary Onslaught']:
        return True
    else:
        return False

def update_wf_data(url = "https://n8k6e2y6.ssl.hwcdn.net/repos/hnfvc0o3jnfvc873njb03enrf56.html"):
    r = requests.get(url)
    soup = bs4.BeautifulSoup(r.text,features="html5lib")
    tables = soup.find("table")

    match = None
    rotation = None

    l = []

    for row in tables.contents[0]:

        if len(row.contents)==1:
            text = row.get_text()
            if "Rotation" in text:
                rotation = text
            else:
                match = text
        elif len(row.contents)==2:
            item = row.contents[0].get_text()
            chance = row.contents[1].get_text()

            #print('%s %s %s %s'%(match,rotation,item,chance))
            l.append([match,rotation,item,chance])

    df = pd.DataFrame(l,columns=["matchdata","Rotation","Item","Chance"])

    df["Planet"],df["Location"],df["Gametype"] = zip(*df['matchdata'].apply(parse_matchdata))
    df["Count"],df["Item"]=zip(*df['Item'].apply(parse_item))
    df['Chance'] = df['Chance'].apply(parse_chance)
    df['ChancePer'] = df['Chance']*df["Count"]
    df["Rotation"]=df["Rotation"].apply(lambda x:x[-1])
    df["Endless"]=df["Gametype"].apply(is_endless_mission)
    #del df['matchdata']

    #df = df[["Planet","Location","Gametype","Rotation","Endless","Item","Count","Chance","ChancePer"]]
    df.to_csv("wf_data.csv")
    return df

def get_top20_for_item(item,count=20):
    if item not in df["Item"].unique():
        return "Error, \"%s\" not a valid item (Check spelling)"%item
    else:
        return str(df[df["Item"]==item].groupby(["Location","Rotation"]).sum()["ChancePer"].sort_values(ascending=False).head(count))
    

def lookup_item(search):
    return "Possible Matches:\n"+"\n".join(["%s, %s%%"%(i[0],i[1]) for i in process.extract(search,unique_items)[:10]])

#df = update_wf_data() 
df = None

try:
    print("Trying to load Warframe Drop table data from file...")
    df = pd.read_csv("wf_data.csv",index_col=0)
    unique_items = df['Item'].unique()
    print("Success!")
except:
    print("No file...")

'''if df is None:
    try:
        print("Trying to load Warframe drop data from site...")
        df = update_wf_data()
        print("Success!")
    except:
        print("Error reading site")

print(get_top20_for_item("Endo"))

'''
