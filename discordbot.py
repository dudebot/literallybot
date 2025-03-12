import discord
import asyncio
import sys
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
from collections import Counter
#from discord.ext import commands


#TODO
#    figure out why i cant continuously update the game name
#    subclass commands.bot and move functions to classes
#    command line access?
#

    
#class discordbot(commands.Bot):


SPAM_TOTAL_POSTS= 10
SPAM_TIME_RANGE = 20
owner_id = '125839498150936576'
bullies = ['95162653059592192','125839498150936576']
posted_danbooru = set()
#posted_gelbooru = set()
posted_e621 = set()
posted_sankaku = set()
headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_11_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/50.0.2661.102 Safari/537.36'}

def test_bad_url(msg):
    return False
    try:
        text = requests.get(msg).text.lower()
        if "republicans" in text or "comedy central" in text or "trevor noah" in text:
            return True
    except:
        pass
    return False

def getproperdate(input):
    date = datetime.datetime.strptime(input,"%Y%m%d")
    #return date.strftime("%b %d, %Y")
    return "%s %s, %s"%(date.strftime("%b"),str(int(date.day)),date.year)

def get_sankaku(tags):
    if any(['loli' in x.lower() for x in tags]):
        print("here")
        return "factually fuck off"
    else:
        tags.append("-loli")
        url = "https://chan.sankakucomplex.com/?tags=%s"%("+".join(tags))
        html = requests.request('GET',url,headers=headers).text
        bs = bs4.BeautifulSoup(html,'html5lib')
        posts = bs.find('div',{'id':'content'})
        html = None
        for p in posts.find_all("span",{'class':'thumb'}):
            url = "https://chan.sankakucomplex.com"+p.find("a").get('href')
            if url in posted_sankaku:
                continue
            html = requests.request('GET',url,headers=headers).text
            bs = bs4.BeautifulSoup(html,'html5lib')
            posted_sankaku.add(url)
            src = None
            for i in bs.find_all("img"):
                if i.get("alt") is None or i.get("alt") is "" or "Sankaku Complex:" in i.get("alt"):
                    continue
                src = i.get("src")
                src = "https:"+src
            if src is None:
                continue
            return src
    return None

def get_e621(tags):
    url = "https://e621.net/post/index/1/%s"%" ".join(tags)
    html = requests.request('GET',url,headers=headers).text
    bs = bs4.BeautifulSoup(html,'html5lib')
    posts = bs.find('div',{'class':'content-post'})
    seen = False
    for p in posts.find_all('div',{'class':None,'id':None}):
        results = p.find_all("span",{'class':'thumb'})
        if len(results)==0:
            continue
        for result in results:
            result = "https://e621.net"+result.find("a").get("href")
            seen = True
            if result in posted_e621:
                continue
            else:
                posted_e621.add(result)
                html = requests.request('GET',result,headers=headers).text
                bs = bs4.BeautifulSoup(html,'html5lib')
                highres = bs.find("a",{'id':"highres"})
                if highres is not None and highres.get("href") is not None:
                    return highres.get("href")
                else:
                    result
                break
    return None

def get_danbooru(tags):
    note=""
    if len(tags)>2:
        tags = tags[:2]
        #note = " (only using first 2 tags)"
    if len(tags)>0:
        html = requests.request('GET',"http://danbooru.donmai.us/posts?tags=%s"%"+".join(tags)).text
        bs = bs4.BeautifulSoup(html,'html5lib')
        posts = bs.find('div',{'id':'posts'})
        if posts is not None:
            articles = posts.find_all("article")
            if len(articles)>0:
                urls = [i.get("data-file-url") for i in articles]
                for url in urls:
                    if url in posted_danbooru:
                        continue
                    else:
                        posted_danbooru.add(url)
                        return url+note
    return None


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
        await client.change_presence(game=discord.Game(name=self.binary_text))
        self.binary_text = ('1' if random.choice([True,False]) else '0') + self.binary_text[:-1]
        await asyncio.sleep(20)
        await self.update_status()
        
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
            
    async def on_server_available(self, server):
        print("Joined server: %s"%server.id)
        self.refresh_log_channel(server)
        
    #async def on_reaction_add(self, reaction, user):
    #    if user.id ==168196162468315136:
    #        await asyncio.sleep(3)
    #        await client.remove_reaction(message, reaction, user)
    
    async def on_message(self,message):
        
        if message.author.name != client.user.name:
            #if 'mech' in message.channel.name or 'star' in message.channel.name or 'colon' in message.channel.name:
            #    for em in message.server.emojis: #jesus fucking christ why cant you typecast from str you fuck
            #        if 'tapir' in str(em):
            #            await client.add_reaction(message, em)
            
            #rate limiting?
            print("%s %s %s"%(str(message.channel.name).encode('ascii', 'backslashreplace'),
                            str(message.author.name).encode('ascii', 'backslashreplace'),
                            str(message.content).encode('ascii', 'backslashreplace')))
            
            has_badword = False
            for badword in self.badwords:
                if badword in message.content.lower():
                    has_badword = True
                    break
            if has_badword:
                await client.delete_message(message)
                msg_to_delete = await client.send_message(message.channel,"shut up you fukkin dummy!")
                await asyncio.sleep(5)
                await client.delete_message(msg_to_delete)
            elif test_bad_url(message.content):
                 await client.delete_message(message)
                 msg_to_delete = await client.send_message(message.channel,"this has forbidden content!")
                 await asyncio.sleep(5)
                 await client.delete_message(msg_to_delete)
            elif message.content.startswith('!help'):
                await client.send_message(message.channel, 
'''!help  - This message
!random   - Picks a random item from a space-separated list
!wtf      - Awesome lol wow! 
!whoami   - Shows your discord id
!danbooru - Shitty 2hu porn
!gelbooru - Shitty bad porn
!e621     - Now thats what im talking about!
!should   - Yes or no answers. Most other interrogatives work in context. YOU MUST OBEY THE BOT OR YOU WILL BE BANNED
!d6       - Dice. Multiple roles are supported up to 9 dice (6d20)
''')
            #elif message.content.startswith('!test'):
            #    hated = [((self.p.plural(word) if pos == 'NN' else word),pos) for word,pos in nltk.tag.pos_tag(re.findall(r"[\w']{2,}", message.content[5:])) if pos in ['NNP','VBG','NNS','NN']]
            #    await client.send_message(message.channel, str(hated))
            #elif message.content.startswith('!refreshserverconfig'):
            #    await client.send_message(message.channel,self.refresh_log_channel(message.server))

            elif message.content.startswith('!kys') and message.author.id == owner_id:
                await client.send_message(message.channel, "bye")
                print(message.author.id)
                exit(0)

            elif message.content.startswith('!random'):
                values = re.split(r'\W',message.content)
                values = [value for value in values if value != ""]
                if len(values) >= 2:
                    values = values[1:]
                await client.send_message(message.channel, str(random.choice(values)))
            
            #elif message.content.startswith('!audio'):
            #    voice = yield from client.join_voice_channel(channel)
            #    player = voice.create_ffmpeg_player('cool.mp3')
            #    player.start()
            
            elif message.content.startswith('!wtf'):
                #await client.add_reaction(message, '\U0001f44d')
                #await client.add_reaction(message, self.costanza)
                await client.add_reaction(message, '\U0001f1fc')
                await client.add_reaction(message, '\U0001f1f9')
                await client.add_reaction(message, '\U0001f1eb')
            
            elif message.content.startswith('!whoami'):
                await client.send_message(message.channel, str(message.author.id)+str(message.author.name))

            elif message.content.startswith("!danbooru"):
                tags = message.content.split()[1:]
                if random.random()<0.07 and any([i in message.author.name.lower() for i in ['laserangel','norris','jacques']]):
                    #tags.append("male_focus")
                    tags = ['male_focus'] + tags
                result = get_danbooru(tags)
                if result is not None:
                    await client.send_message(message.channel,result)
            elif message.content.startswith("!e621"):
                tags = message.content.split()[1:]
                if  random.random()<0.07 and any([i in message.author.name.lower() for i in ['laserangel','norris','jacques']]):
                    tags.append("male_focus")
                result = get_e621(tags)
                if result is not None:
                    await client.send_message(message.channel,result)
            elif message.content.lower().startswith("!sankaku"):
                tags = message.content.split()[1:]
                if  random.random()<0.07 and any([i in message.author.name.lower() for i in ['laserangel','norris','jacques']]):
                    tags.append("male_focus")
                result = get_sankaku(tags)
                if result is not None:
                    r = requests.get(result,headers=headers)
                    with open("test.jpg",'wb') as f:
                        f.write(r.content)
                        await client.send_file(message.channel, "test.jpg")

            elif message.content.startswith("!test"):
#                await bot.send_file(channel, "filepath.png", content="...", filename="...")
#                await client.send_file(message.channel, "test.jpg")
                msgToDelete = await client.send_message(message.channel,"test")
                await asyncio.sleep(5)
                await client.delete_message(msgToDelete)
            elif message.content.startswith('!d') and all([str.isdigit(s) for s in message.content[2:]]):
                await client.send_message(message.channel, str(random.randint(1,int(message.content[2:]))))
            
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
                    await client.send_message(message.channel, str(sum))
                except:
                    print("error")
                    pass
            #elif message.content.lower().startswith('!should') or message.content.lower().startswith('!will') or message.content.lower().startswith('!does') or message.content.lower().startswith('!can') or message.content.lower().startswith('!is') or message.content.lower().startswith('!has'):
            elif  message.content.startswith('!') and \
                  message.content.lower()[1:message.content.find(" ")] in ['should','will','does','is','has','can','may','could','might','shall','would','are']:
                if random.random()>0.5:
                    await client.send_message(message.channel, "No")
                else:
                    await client.send_message(message.channel, "Yes")
            
            
            else:#catchall for everything else
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
                            #await client.send_message(message.channel,"WOW already posted on %s dude."%getproperdate(repost_date))
                if self.update_lastposts(message.author):
                    await client.send_message(message.channel,"WTF I hate %s now"%message.author.name)
                if random.random() > 0.9999 and (self.last_wtf + 900 < int(time.time())):
                    tagged = nltk.tag.pos_tag(re.findall(r"[\w']{2,}", message.content))
                    #hated = [(word,pos) for word,pos in tagged if pos in ['NNP','VBG','NNS','NN']]
                    hated = [(word,pos) for word,pos in tagged if pos in ['NNP','VBG','NNS']]
                    if len(hated) > 0:
                        self.last_wtf = int(time.time())
                        hate = random.choice(hated)
                        text = self.p.plural(hate[0]) if hate[1] == 'NN' else hate[0]
                        #pass
                        await client.send_message(message.channel,"WTF I hate %s now"%text)
        
        '''
        if message.content.startswith('!countmsg'):
            counter = 0        
            tmp = await client.send_message(message.channel, 'Calculating messages...')
            async for log in client.logs_from(message.channel, limit=100):
                if log.author == message.author:
                    counter += 1

            await client.edit_message(tmp, 'You have {} messages.'.format(counter))
        elif message.content.startswith('!sleep'):
            await asyncio.sleep(5)
            
        '''

    '''
    
    async def on_member_join(self,member):
        print("new member joined: "+member.name.encode('ascii', 'ignore'))
        

    async def on_member_remove(self,member):
        print("member left server: "+member.name.encode('ascii', 'ignore'))


    '''

    async def on_member_join(self,member):
        await client.send_message(self.log_channel[member.server.id], "<@%s>, you are not welcome here please leave immediately"%(member.id))
    
    async def on_member_remove(self,member):
        await client.send_message(self.log_channel[member.server.id], "We sure told <@%s> AKA %s"%(member.id, member.name))

    
    async def on_member_update(self,before, after):
        if before.name is not None and after.name is not None and before.name != after.name:
            await client.send_message(self.log_channel[before.server.id], "%s changed their name to %s"%(before.name,after.name))
            


    async def on_voice_state_update(self,before, after):
        if (before.name in ["AIRHORN SOLUTIONS"]):
            return
        if before.voice_channel is None or before.server.id != after.server.id:
            await client.send_message(self.log_channel[after.server.id], "%s joined **%s**"%(after.name,after.voice_channel.name))
        if (after.voice_channel is None and before.voice_channel is not None) or before.server.id != after.server.id:
            await client.send_message(self.log_channel[before.server.id], "%s left **%s**"%(before.name,before.voice_channel.name))
        if before.voice_channel is not None and after.voice_channel is not None and before.voice_channel != after.voice_channel and before.server.id == after.server.id:
            await client.send_message(self.log_channel[before.server.id], "%s moved from **%s** to **%s**"%(after.name,before.voice_channel.name,after.voice_channel.name))
            
        
#sys.stdin.read() #...
client = discordbot()
client.run('redacted')
