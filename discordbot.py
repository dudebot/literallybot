import discord
import asyncio
import sys
import os
import re
import random
import datetime
import time
import collections
import nltk
import inflect
import copy
import requests
import bs4
from discord import File
from collections import Counter
import sys
import base64
import images
import warframe
from py_expression_eval import Parser


#from discord.ext import commands

#add slash command functionality
#    add the api integration with the dudebot account for danbooru
#    add search by order:random
#    polish role editing system
#    pull out more functions so this script is just a basic runloop
#    figure out why i cant continuously update the game name
#    subclass commands.bot and move functions to classes
#    command line access?
#    logging

    
#class discordbot(commands.Bot):



reddit = {}
reddit['yikes']=['have sex','rent free','seethe']
reddit['cope']=['have sex','yikes','cringe']
reddit['seethe']=['have sex','cope','rent free']
reddit['cringe']=['have sex','seethe','yikes']
reddit['rent free']=['have sex','cringe','cope']
reddit['have sex']=['dilate']
reddit['dilate']=['rent free','yikes','cope','seethe','cringe']

def generate_karma(string):
    for key in reddit.keys():
        if key in string.lower():
            return random.choice(reddit[key])



nitro_shills=[351783699853082625,
	125839498150936576,
	126463031566663680,
	133011289298567169,
        143881097971892224,
        92033561426661376,
        125851531546198017]

good_pills = ['kongpilled',
         'basepilled',
         'redpilled',
         'blackpilled',
         'norrispilled'
         ]

headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_11_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/50.0.2661.102 Safari/537.36'}
parser = Parser()

SPAM_TOTAL_POSTS= 10
SPAM_TIME_RANGE = 20
owner_id = 125839498150936576 #dudebot
admin_ids = [125839498150936576,#dudebot
            129315219653525504, #spadoop
            351783699853082625, #norris main
            125834268860612608 #norris phone
            ]
main_server = 125817769923969024 #kong
bullies = [95162653059592192,125839498150936576] #stoopit?

def clean_exit():
    sys.exit(0)

#returns an array of split options
#todo use and or or optionally while keeping the regex split sane
def get_options(options):
  if re.search(r',? ?\bor\b ?|, ?',options.lower()) is not None:
    values = re.split(r',? ?\bor\b ?|, ?',options.lower())
  elif ' ' in options:
    values = options.split(' ')
  else:
    values = re.split(r'\W',options)
  return [value for value in values if value != ""]

class discordbot(discord.Client):

    log_channel={}
    wakeup_started = False
    #badwords = ['dsfargeg','amd','credit card','apy']
    badwords = ['dsfargeg']
    binary_text = "01010101"
    urls = {}
    url_re = re.compile(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$#-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+')
    lastposts = {}
    p = inflect.engine()
    bullying = 0
    bully_target = ''
    last_wtf = 0
    
    '''b'general' b'dudebot' b'w \\U0001f1fc'
b'general' b'dudebot' b't \\U0001f1f9'
b'general' b'dudebot' b'f \\U0001f1eb'
b'general' b'dudebot' b'i \\U0001f1ee'
b'general' b'dudebot' b'h \\U0001f1ed'
b'general' b'dudebot' b'a \\U0001f1e6'
    '''
    
    
    #helper function to check the timing of user posts.
    #if they hit this function too quickly, it will return TRUE
    def update_lastposts(self,poster):
        if poster.id not in self.lastposts:
            self.lastposts[poster.id] = collections.deque([int(time.time())]+[0]*(SPAM_TOTAL_POSTS-2)) #subtracting 2 because when we add the current time on each iteration, it's n+1 long
            return False
        else:
            self.lastposts[poster.id].appendleft(int(time.time()))
            if self.lastposts[poster.id].pop() > int(time.time()) - SPAM_TIME_RANGE:
                self.lastposts[poster.id] = collections.deque([0]*(SPAM_TOTAL_POSTS))
                return True
    
    
    def load_urls_from_file(self,filename="urls.txt"):
        with open(filename) as input:
            count = 0
            for url in input:
                url = url.strip().split("\t")
                date = self.add_url(url[1],url[0])
                count+=1
            print(self.urls.keys())
                
    #checks if the url string is in the dates datastructure,
    #if it is, it returns the date, otherwise None
    #adds the url to self.urls and trims self.urls if a new date is seen and it's too big
    def add_url(self, url_string, posted_date = ""):
        #used by a loader to populate the urls datastructure via file
        #otherwise will be the current date
        if posted_date == "":
            posted_date = datetime.date.today().strftime("%Y%m%d")
        #find the most recent one
        dates = sorted(self.urls.keys(),reverse=True)
        found = False
        found_date = ""
        #Test if URL exists
        for date in dates:
            if url_string in self.urls[date]:
                found = True
                found_date = date
                break
        #trim the length of the array if it's both a new element, and it's a new day
        #for a week, we want to delete the 7th day to make 6, since we will add a new day below
        if found and posted_date not in dates and len(dates) >= 90:
            del self.urls[dates[0]]
        
        #Add the URL        
        if posted_date not in dates:
            self.urls[posted_date]=set()
        self.urls[posted_date].add(url_string)
        if found:
            return found_date
        else:
            return None

    async def update_status(self):
        game = discord.Game(self.binary_text)
        await client.change_presence(activity=game)
        self.binary_text = ('1' if random.choice([True,False]) else '0') + self.binary_text[:-1]
        #await asyncio.sleep(20)
        #await self.update_status()
        
    async def on_socket_closed(self):
        print("socket closed")
        sys.exit(0)
    
    async def on_ready(self):
        print('Logged in as')
        print(client.user.name)
        print(client.user.id)
        
        #print("Logged into servers:")        
        if not self.wakeup_started:
            self.loop.create_task(self.update_status())
            
        self.load_urls_from_file()

    def refresh_log_channel(self, server):
        for channel in server.channels:
            if channel.name.lower() =="log":
                found = True
                self.log_channel[server.id]=channel
                print('Found channel for server: %s'%(channel.id))
        if not found:
            print('WARN: channel NOT found for server: %s'%(server.id))
            return 0
        return self.log_channel[server.id].id
            
    async def on_guild_available(self, server):
        print("Joined server: %s"%server.id)
        print("Attempting to find log channels:")
        self.refresh_log_channel(server)
        print("log channels: %s"%(str(self.log_channel)))
        
    async def on_message(self,message):
        #print(message.id)
        #print(message.channel.id)
        if message.author.name != client.user.name:
            #if 'mech' in message.channel.name or 'star' in message.channel.name or 'colon' in message.channel.name:
            pass
            if type(message.channel) is discord.DMChannel:
                print("%s %s %s"%('direct message',
                            str(message.author.name).encode('ascii', 'backslashreplace'),
                            str(message.content).encode('ascii', 'backslashreplace')))
            else:
                print("%s %s %s"%(str(message.channel.name).encode('ascii', 'backslashreplace'),
                            str(message.author.name).encode('ascii', 'backslashreplace'),
                            str(message.content).encode('ascii', 'backslashreplace')))

            ''' 
            has_badword = False

            for badword in self.badwords:
                if badword in message.content.lower():
                    has_badword = True
                    break
            if has_badword:
                await message.delete
                msg_to_delete = await message.channel.send("shut up you fukkin dummy!")
                await asyncio.sleep(5)
                await delete_message.delete()
            elif test_bad_url(message.content):
                 await message.delete
                 msg_to_delete = await message.channel.send("this has forbidden content!")
                 await asyncio.sleep(5)
                 await delete_message.delete()
            '''

            if message.content.startswith('!help'):
                await message.channel.send('''!help  - This message
!random   - Picks a random item from a space-separated list
!wtf      - Awesome lol wow! 
!squish   - nyaa!
!aaa      - suffering
!ding     - :relieved:
!blanket  - mmmmmmmmmmm
!whoami   - Shows your discord id
!setrole  - Adds or removes available roles
!wfsearch - Performs a case sensitive item search against the warframe drop tables
!danbooru - Danbooru image search
!sankaku  - Sankaku Channel search (dont make me fucking delete this you punk)
!e621     - Now thats what im talking about!
!should   - Yes or no answers. Most other interrogatives work in context. YOU MUST OBEY THE BOT OR YOU WILL BE BANNED
!d6       - Dice. Multiple roles are supported up to 9 dice (eg: !6d20)
!suggest  - Send a note to the Creator for ideas on how to make me better
!worthless- Holy shit dudebot what the fuck is wrong with you jesus christ
''')
            #elif message.content.startswith('!test'):
            #    hated = [((self.p.plural(word) if pos == 'NN' else word),pos) for word,pos in nltk.tag.pos_tag(re.findall(r"[\w']{2,}", message.content[5:])) if pos in ['NNP','VBG','NNS','NN']]
            #    await message.channel.send( str(hated))
            #elif message.content.startswith('!refreshserverconfig'):
            #    await message.channel.send(self.refresh_log_channel(message.server))
            elif message.content.startswith('!kys') and message.author.id in admin_ids:
                await message.channel.send( "bye")
                clean_exit()
            elif message.content.startswith('!superkys') and message.author.id in admin_ids:
                await message.channel.send( "superbye")
                #prepare_exit()
                res = os.popen("systemctl stop bot").read()
                clean_exit() #lel
            elif message.content.lower().startswith("!suggest"):
                await message.delete()
                await message.channel.send("I deleted that suggestion because it was really dumb. Don't waste my time again you n-word")
            elif message.content.lower()=="same":
                await message.channel.send( "Accurate")
            elif "based" in message.content.lower() and len(message.content)<8 and not message.lower().startswith("!based"):
                if  message.author.premium_since is not None:
                #profile = await message.author.profile()
                #if  profile.is_premium:
                    await message.channel.send( "and "+random.choice(good_pills))
                else:
                    #await message.delete()
                    msg_to_delete = await message.channel.send("Based? Based on what?")
                    await asyncio.sleep(5+5*random.random())
                    await msg_to_delete.delete()

            elif message.content.lower()=="cringe":
                await message.channel.send( "and "+random.choice(bad_pills))

            elif message.content.lower() in reddit.keys():
                await message.channel.send(generate_karma(message.content.lower()))
            elif message.content.startswith("!wfsearch"):
                await message.channel.send(warframe.get_top20_for_item(message.content[10:]))
            elif message.content.startswith("!wflookup"):
                await message.channel.send(warframe.lookup_item(message.content[10:]))

            elif message.content.startswith("!wfupdate") and message.author.id in admin_ids:
                warframe.update_wf_data(message.content[9:])
                await message.channel.send("attempted to update warframe drop data, you'll probably need to kms and i'll kyp")
            elif "nezha" in message.content.lower():
                await message.channel.send("Nezha is a ||cute|| trap")

            elif message.content.startswith("!epictendies") and message.author.id in admin_ids:
                rolename = message.content[13:]
                print(rolename)
                for s in client.guilds:
                    if s.id==main_server:
                        server = s
                        break
                #get the server role
                role = None
                for r in server.roles:
                    #print(r.name)
                    if r.name.lower()==rolename.lower():
                        role=r
                        break

                if role is not None:
                    for m in s.members:
                        try:  
                            #await m.add_roles(role)
                            await m.remove_roles(role)
                            #await m.send("You have been automatically added to the role: %s. You can remove this role by sending me the command !setrole -%s. Be sure to tell dudebot to fuck off on your earliest convenience!"%(r.name,r.name))
                            print("%s Success"%m.name)
                        except:
                            print("%s Fail"%m.name)
                        #print(m.name)
                    #await message.author.add_roles(role)
                    #await message.channel.send("Added to role: %s"%r.name)

            elif message.content.startswith("!setrole"):
                rolename = message.content[10:].lower()
                role_option = message.content[9]
                if role_option not in ["+","-"]:
                    await message.channel.send("Use a + or - to add or remove the role (eg: !setrole +Kinography)")
                elif rolename not in whitelist_roles:
                    await message.channel.send("Role not in whitelist %s"%rolename)
                else:
                    for s in client.guilds:
                        if s.id==main_server:
                            server = s
                            break
                    #get the server role
                    role = None
                    for r in server.roles:
                        print(r.name)
                        if r.name.lower()==rolename:
                            role=r
                            break
    
                    if role is not None:
                        if role_option=="+":
                            await message.author.add_roles(role)
                            await message.channel.send("Added to role: %s"%r.name)
                        if role_option=="-":
                            await message.author.remove_roles(role)
                            await message.channel.send("Removed from role: %s"%r.name)

                    else:
                        await message.channel.send("Could not find role: %s"%rolename)





            elif type(message.channel) == discord.channel.DMChannel and all([i in message.content.lower() for i in ['kong','strong']]):
                #get server
                server = None
                for s in client.guilds:
                    if s.id==main_server:
                        server = s
                        break
                #get the server role
                role = None
                for r in server.roles:
                    if r.name=="kong":
                        role=r
                        break
                
                #look up user in main_server 
                user = None
                for u in server.members:
                    if u.id == message.author.id:
                        user = u
                        break

                #apply the server role to user
                #print(user.roles)
                if user is not None and role is not None:
                    await user.add_roles(role)
                    await message.channel.send( "kong ! stong !!")
                else:
                    await message.channel.send( "Something went wrong because dudebot is fucking incompetent, please ping him and tell him he's fucktarded and you never should have seen this error message")
            elif message.content.startswith('!order'):
                values = get_options(message.content[6:].strip())
                if len(values) >= 1:
                    random.shuffle(values)
                    output_txt = "\n".join(["%s) %s"%(i+1,option) for (i,option) in enumerate(values)])
                    await message.channel.send(output_txt)
            elif message.content.startswith('!random'):
                values = get_options(message.content[7:].strip())
                if len(values) >= 1:
                    if random.random()<0.05:
                         await message.channel.send( "All of these options are terrible. Please think about your life and try later.")
                    else:
                        await message.channel.send( str(random.choice(values)))
                else:
                    await message.channel.send( "ur supposed 2 give choices dummy")
            
            #elif message.content.startswith('!audio'):
            #    voice = yield from client.join_voice_channel(channel)
            #    player = voice.create_ffmpeg_player('cool.mp3')
            #    player.start()

            elif message.content.startswith('!wtf'):
                await message.add_reaction('\U0001f1fc')
                await message.add_reaction('\U0001f1f9')
                await message.add_reaction('\U0001f1eb')
            elif message.content.startswith("!blanket?"):
                snuggles = random.choice([i for i in os.listdir("media") if i.startswith("snug")])
                await message.author.send(file=File(os.path.join("media",snuggles)))

            #todo use the filenames of any files in the media folder to make a list of commands
            #also make a command to update the list of commands from the contents of the folder
            elif message.content.startswith("!squish"):
                await message.channel.send(file=File("media/squish.webm"))
            elif message.content.startswith("!ding"):
                await message.channel.send(file=File("media/dingdingdoo.webm"))
            elif message.content.startswith("!aaa"):
                await message.channel.send(file=File("media/aaaaaaaa_hd.mp4"))
            elif message.content.startswith("!nigga"):
                await message.channel.send(file=File("media/nwordcute.mp4"))
            elif message.content.startswith("!pog"):
                await message.channel.send(file=File("media/poggers.mp4"))
            elif message.content.startswith("!heyhey"):
                await message.channel.send(file=File("media/kaguya-sama.mp4"))
            elif message.content.startswith("!based"):
                await message.channel.send(file=File("media/based.mp4"))
            elif message.content.startswith("!yahallo"):
                await message.channel.send(file=File("media/yahallo.mp4"))
            elif message.content.startswith("!shrimple"):
                await message.channel.send(file=File("media/shrimple.mp4"))
            elif message.content.startswith("!hah") or  message.content.startswith("!joneshorn"):
                await message.channel.send(file=File("media/THE_JONES_LAUGH.mp3"))
            elif message.content.startswith("!hah") or  message.content.startswith("!arbys"):
                await message.channel.send(file=File("media/arbys.mp4"))

            elif message.content.startswith('!whoami'):
                await message.channel.send( str(message.author.id)+str(message.author.name))

            elif message.content.startswith("!danbooru"):
                tags = message.content.split()[1:]
                result = images.get_danbooru(tags)
                if result is not None:
                    await message.channel.send(f"{result} for tags {tags}")
                else:
                    await message.channel.send(f"No results found for tags: {tags} :'(")
            elif message.content.startswith("!e621"):
                tags = message.content.split()[1:]
                result = images.get_e621(tags)
                if result is not None:
                    await message.channel.send(f"{result} for tags {tags}")
                else:
                    await message.channel.send(f"No results found for tags: {tags} :'(")
            elif message.content.lower().startswith("!sankaku"):
                tags = message.content.split()[1:]
                if 'loli' in [i.lower() for i in tags]:
                    await message.channel.send("yeah im gonna need you to fuck right off asap")
                else:
                    result = images.get_sankaku(tags)
                    if result is not None:
                        r = requests.get(result,headers=headers)
                        with open("temp.jpg",'wb') as f:
                            f.write(r.content)
                            await message.channel.send(file=File("temp.jpg"))
                    else:
                        await message.channel.send(f"No results found for tags: {tags} :'(")


            elif message.content.startswith('!d') and all([str.isdigit(s) for s in message.content[2:]]):
                await message.channel.send( str(random.randint(1,int(message.content[2:]))))
            elif message.content.startswith('!') and len(message.content) >=4 and \
                             message.content[1] in ['1','2','3','4','5','6','7','8','9'] and \
                             message.content[2].lower()=='d':
                print(message.content)
                print(message.content[1])
                sum = 0
                try:
                    rolls = int(message.content[1])
                    die = int(message.content[3:])
                    for i in range(0,rolls):
                        sum+=random.randint(1,die)
                    await message.channel.send( str(sum))
                except:
                    print("error")
                    pass
            #elif message.content.lower().startswith('!should') or message.content.lower().startswith('!will') or message.content.lower().startswith('!does') or message.content.lower().startswith('!can') or message.content.lower().startswith('!is') or message.content.lower().startswith('!has'):
            elif  message.content.startswith('!') and \
                  message.content.lower()[1:message.content.find(" ")] in ['should','would','could','can','will','does','may','might','shall','must','is','am','are','has','had','have','were','was','do','did']: #all modal verbs plus "is" conjugations (and do)
                if random.random()>0.5:
                    await message.channel.send( "No")
                else:
                    await message.channel.send( "Yes")
            

            elif message.content.startswith('!logchannel') and message.author.id in admin_ids:
                counter = 0
                tmp = await message.channel.send( 'Calculating messages...')
                with open("%s_%s.txt"%("Kong",message.channel.id),'w') as logf:
                    async for log in message.channel.history(limit=999999999):
                        if log.author == message.author:
                            counter += 1
                        logf.write("%s\t%s\t%s\n"%(log.author.id,log.created_at,log.content.replace("\n","\\n")))
                await tmp.edit(content='You have {} messages.'.format(counter))
            elif message.content=="!getguild":
                await message.channel.send("%s type:%s"%(message.guild.id,str(type(message.guild.id))))
            elif message.content[0]=="\"" and message.content[-1]=="\"" and message.guild.id==191762537438511104:
                #await message.delete()
                await message.channel.send("> "+ "".join([i[1].upper() if i[0]%2==1 else i[1].lower() for i in enumerate(message.content[1:-1].replace(' ','  '))]).replace('  ',' ')+'\n%s\n%s, %s'%(
                        message.author.nick if message.author.nick is not None else message.author.name,
                        random.choice(["Someone Important","Chief Furry","Alcoholic","Literally Who","Basically Hitler","Mayor of Foofgens","Peta","Some fuckin weeb"]),
                        datetime.datetime.today().year))
            elif message.content.startswith("!quoteme"):
                #await message.delete()
                await message.channel.send("> "+ "".join([i[1].upper() if i[0]%2==1 else i[1].lower() for i in enumerate(message.content[8:].replace(' ','  '))]).replace('  ',' ')+'\n%s\n%s, %s'%(
                	message.author.nick if message.author.nick is not None else message.author.name,
                	random.choice(["Founder of Kong","Chief Furry","A Very Experienced Person (sexually I mean)","Literally Who","Basically Hitler"]),
                	datetime.datetime.today().year))
            #elif message.author.id==125839498150936576 and message.channel.id==275284343335944192 and len(message.content.split()) < 10: #dudebot, pol channel
            elif message.author.id==136710877289119744 and message.channel.id==275284343335944192 and len(message.content.split()) < 10: #spud, pol channel
                await message.channel.send("Error, your message did not satisfy the minimum requirements (%s/10 tokens). Please try again."%(len(message.content.split())))
                await message.delete()

            else:#catchall for everything else
                math_res = None
                matches_original = False
                try:
                    math_res = parser.parse(message.content).evaluate({}) #will error if not valid
                    math_res_type = str(type(math_res)) #used to remove "quoted strings"

                    direct_value = float(message.content)
                    print(direct_value)
                    matches_original = direct_value==math_res #if error on parsing, will default to false due to line not run

                except: pass
                if math_res is not None and "\"" not in message.content and '\'' not in message.content and math_res_type!="<class 'method'>" and not matches_original:
                    await message.channel.send(str(math_res))
                urls = set(self.url_re.findall(message.content))
                if(len(urls)!=0):
                    with open("urls.txt",'a') as output:
                        repost_date = None
                        for url in urls:
                            date = self.add_url(url)
                            if date is not None:
                                repost_date = date
                            #we want to publish all URLs now
                            output.write("%s\t%s\n"%(datetime.date.today().strftime("%Y%m%d"),url))
                        #if repost_date is None and str(message.author.name).lower()=='kaelus':
                        #    repost_date = (datetime.datetime.today()-datetime.timedelta(random.randint(40,120))).strftime("%Y%m%d")     
                        if repost_date is not None:
                            print(repost_date)
                            #await message.channel.send("WOW already posted on %s dude."%getproperdate(repost_date))
                if self.update_lastposts(message.author):
                    await message.channel.send("WTF I hate %s now"%message.author.name)
                if random.random() > 0.9999 and (self.last_wtf + 900 < int(time.time())):
                    tagged = nltk.tag.pos_tag(re.findall(r"[\w']{2,}", message.content))
                    #hated = [(word,pos) for word,pos in tagged if pos in ['NNP','VBG','NNS','NN']]
                    hated = [(word,pos) for word,pos in tagged if pos in ['NNP','VBG','NNS']]
                    if len(hated) > 0:
                        self.last_wtf = int(time.time())
                        hate = random.choice(hated)
                        text = self.p.plural(hate[0]) if hate[1] == 'NN' else hate[0]
                        #pass
                        await message.channel.send("WTF I hate %s now"%text)

 

    '''
    async def on_member_join(self,member):
        print("new member joined: "+member.name.encode('ascii', 'ignore'))
        

    async def on_member_remove(self,member):
        print("member left server: "+member.name.encode('ascii', 'ignore'))


    '''

    async def on_member_join(self,member):
        if member.guild.id==125817769923969024:
             await self.log_channel[member.guild.id].send("<@%s>, DM this bot with a \"Kong Strong!\" for the kong role."%(member.id))
        else:
             await self.log_channel[member.guild.id].send("Welcome <@%s>"%(member.id))
    async def on_member_remove(self,member):
        await self.log_channel[member.guild.id].send("We sure told <@%s> AKA %s"%(member.id, member.name))

    
    async def on_member_update(self,before, after):
        if before.name is not None and after.name is not None and before.name != after.name:
            await self.log_channel[before.guild.id].send("%s changed their name to %s"%(before.name,after.name))
            



    async def on_voice_state_update(self,member,before, after): 
        if (member.name in ["AIRHORN SOLUTIONS"]): #ignore bots
            return
        if (after.channel is not None and "Voice Chat" in after.channel.name) or (before.channel is not None and 'Voice Chat' in before.channel.name): #do not post info about moving to and from the admin chat
            return
        if before.channel is None and before.channel != after.channel:
            await self.log_channel[after.channel.guild.id].send("%s joined **%s**"%(member.name,after.channel.name))
        if after.channel is None and before.channel != after.channel:
            await self.log_channel[before.channel.guild.id].send("%s left **%s**"%(member.name,before.channel.name))
        if before.channel is not None and after.channel is not None and before.channel != after.channel and before.channel.guild.id == after.channel.guild.id:
            await self.log_channel[before.channel.guild.id].send("%s moved from **%s** to **%s**"%(member.name,before.channel.name,after.channel.name))
            
        
#intents = discord.Intents(messages=True, guilds=True)
intents = discord.Intents.all()
client = discordbot(intents=intents)
# load client secret from discord_key.txt
with open("discord_key.txt",'r') as f:
    key = f.read().strip()

client.run(key)
