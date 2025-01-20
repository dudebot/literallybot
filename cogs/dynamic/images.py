import requests
import bs4

posted_sankaku=set()
posted_e621=set()
posted_danbooru=set()
headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_11_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/50.0.2661.102 Safari/537.36'}


def load_history():
    pass

def save_history():
    pass

def get_sankaku(tags):
    tags.append("-loli")  # i hate that this is necessary
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
        if src is not None:
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


danbooru_base = "http://danbooru.donmai.us"
def get_danbooru(tags):
    note=""
    if len(tags)>2:
        tags = tags[:2]
    if len(tags)>0:
        html = requests.request('GET',f"{danbooru_base}/posts?tags=%s"%"+".join(tags)).text
        bs = bs4.BeautifulSoup(html,'html5lib')
        posts = bs.find('div',{'id':'posts'})
        if posts is not None:
            articles = posts.find_all("article")
            if len(articles)>0:
                urls = [f"{danbooru_base}{i.find('a').get('href')}" for i in articles]
                urls = [i[:i.rfind("?q=")] for i in urls]
                for url in urls:
                    if url in posted_danbooru:
                        continue
                    else:
                        posted_danbooru.add(url)
                        return url+note
    return None
