import argparse
import json
import re
import sys
import os
import xml.etree.ElementTree as ET
from urllib.parse import urlparse, parse_qs
from email.utils import parsedate_to_datetime

import requests
import questionary 


try:
    from mutagen.mp3 import MP3
    from mutagen.id3 import (
        ID3, APIC, TIT2, TPE1, TALB, COMM, TDRC, TCON, TCOP, 
        WOAR, TRCK, TPOS, TXXX, TCMP, ID3NoHeaderError
    )
except ImportError:
    print("Error: Required libraries are missing.")
    print("Please run: pip install -r requirements.txt")
    sys.exit(1)


HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Accept': '*/*',
    'Connection': 'keep-alive'
}

NAMESPACES = {
    'itunes': 'http://www.itunes.com/dtds/podcast-1.0.dtd',
    'content': 'http://purl.org/rss/1.0/modules/content/'
}

def get_ids_from_url(url):
    show_match = re.search(r'id(\d+)', url)
    show_id = show_match.group(1) if show_match else None
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    episode_id = params.get('i', [None])[0]
    return show_id, episode_id

def get_episode_details(episode_id):
    url = f"https://itunes.apple.com/lookup?id={episode_id}"
    try:
        r = requests.get(url, headers=HEADERS)
        data = r.json()
        if data['resultCount'] > 0:
            res = data['results'][0]
            return res.get('trackName'), res.get('feedUrl'), res.get('collectionName')
    except:
        pass
    return None, None, None

def get_show_details(show_id):
    url = f"https://itunes.apple.com/lookup?id={show_id}"
    try:
        r = requests.get(url, headers=HEADERS)
        data = r.json()
        if data['resultCount'] > 0:
            return data['results'][0]['feedUrl'], data['results'][0]['collectionName']
    except:
        pass
    return None, None

def scrape_apple_metadata(url):
    print("[-] Fetching show information...")
    meta = {
        'title': None, 'genres': [], 'description': '', 
        'rating': None, 'url': url, 'copyright': '',
        'content_rating': None, 'host': None, 'frequency': None, 'website': None,
        'thumbnail_url': None, 'date_modified': None
    }
    jsonld_summary = None
    
    try:
        response = requests.get(url, headers=HEADERS)
        response.raise_for_status()
        text = response.text
        
        jsonld_pattern = r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>'
        jsonld_matches = re.findall(jsonld_pattern, text, re.DOTALL)
        
        for jsonld_str in jsonld_matches:
            try:
                jsonld_data = json.loads(jsonld_str)
                if jsonld_data.get('@type') in ['CreativeWorkSeries', 'PodcastSeries', 'PodcastEpisode']:
                    jsonld_summary = jsonld_data
                    if 'thumbnailUrl' in jsonld_data: meta['thumbnail_url'] = jsonld_data['thumbnailUrl']
                    if 'dateModified' in jsonld_data: meta['date_modified'] = jsonld_data['dateModified']
                    if not meta['title'] and 'name' in jsonld_data: meta['title'] = jsonld_data['name']
                    if not meta['description'] and 'description' in jsonld_data: meta['description'] = jsonld_data['description']
                    if not meta['genres'] and 'genre' in jsonld_data: meta['genres'] = jsonld_data['genre']
                    break
            except json.JSONDecodeError:
                continue
        
        pattern = r'<script type="application/json" id="serialized-server-data">(.+?)</script>'
        match = re.search(pattern, text, re.DOTALL)
        
        if match:
            try:
                raw_data = json.loads(match.group(1))
                if isinstance(raw_data, list) and len(raw_data) > 0:
                    data = raw_data[0].get('data', {})
                    shelves = data.get('shelves', [])
                    for shelf in shelves:
                        if shelf.get('contentType') in ['showHeaderRegular', 'resizingProductHero']:
                            items = shelf.get('items', [])
                            if items:
                                show = items[0]
                                meta['title'] = show.get('title') or meta['title']
                                meta['description'] = show.get('description') or meta['description']
                                meta['website'] = show.get('websiteUrl')
                                meta['frequency'] = show.get('releaseFrequency')
                                meta['copyright'] = show.get('copyright')
                                meta['genres'] = show.get('genreNames', []) or meta['genres']
                                cr = show.get('contentRating')
                                if cr == 'explicit': meta['content_rating'] = '1'
                                elif cr == 'clean': meta['content_rating'] = '2'
                                else: meta['content_rating'] = '0'

                        if shelf.get('contentType') == 'ellipse' and shelf.get('title') == 'Hosts & Guests':
                            host_items = shelf.get('items', [])
                            hosts = [h.get('title') for h in host_items if 'subtitles' in h and 'Host' in h['subtitles']]
                            if hosts: meta['host'] = ", ".join(hosts)

                        if shelf.get('contentType') == 'ratings':
                            items = shelf.get('items', [])
                            if items and 'ratingAverage' in items[0]:
                                r = items[0]
                                meta['rating'] = f"Rating: {r['ratingAverage']}/5 ({r['totalNumberOfRatings']} ratings)"
            except: pass

        if not meta['copyright']:
            copy_match = re.search(r'Copyright\s*</div>\s*<div[^>]*>\s*([^<]+)\s*</div>', text)
            if copy_match: meta['copyright'] = copy_match.group(1).strip()

    except Exception:
        print("[!] Warning: Could not fetch detailed metadata.")
    
    return meta, jsonld_summary

def save_summary_file(folder, show_title, jsonld_data, apple_meta):
    description = apple_meta.get('description', '')
    if not description: return
    summary_path = os.path.join(folder, "summary.txt")
    try:
        with open(summary_path, 'w', encoding='utf-8') as f:
            f.write(description)
    except: pass

def download_file(url, folder, filename):
    filename = re.sub(r'[\\/*?:"<>|]', "", filename)
    filepath = os.path.join(folder, filename)
    
    if os.path.exists(filepath):
        print(f"[-] Skipping: {filename} (Exists)")
        return filepath

    print(f"[*] Downloading: {filename}")
    try:
        with requests.get(url, headers=HEADERS, stream=True) as r:
            r.raise_for_status()
            total_length = r.headers.get('content-length')
            with open(filepath, 'wb') as f:
                if total_length is None:
                    f.write(r.content)
                else:
                    dl = 0
                    total_length = int(total_length)
                    for chunk in r.iter_content(chunk_size=8192):
                        dl += len(chunk)
                        f.write(chunk)
                        done = int(50 * dl / total_length)
                        percent = int(100 * dl / total_length)
                        sys.stdout.write(f"\r[{'=' * done}{' ' * (50-done)}] {percent}%")
                        sys.stdout.flush()
            sys.stdout.write("\n")
        return filepath
    except:
        sys.stdout.write("\n")
        print(f"[!] Failed to download file.")
        return None

def add_tags(filepath, metadata):
    try:
        try:
            audio = MP3(filepath, ID3=ID3)
        except ID3NoHeaderError:
            audio = MP3(filepath)
            audio.add_tags()
        if audio.tags is None: audio.add_tags()

        def set_text_frame(frame_cls, text, encoding=3):
            audio.tags.delall(frame_cls.__name__)
            audio.tags.add(frame_cls(encoding=encoding, text=text))

        audio.tags.delall('USLT')
        audio.tags.delall('SYLT')

        if metadata.get('title'): set_text_frame(TIT2, metadata['title'])
        if metadata.get('album'): set_text_frame(TALB, metadata['album'])
        
        artist = metadata.get('host') if metadata.get('host') else metadata.get('author')
        if artist: set_text_frame(TPE1, artist)

        if metadata.get('genres'):
            g = metadata['genres']
            genre_text = " / ".join(g) if isinstance(g, list) else g
            if 'Podcast' not in genre_text: genre_text = f"Podcast; {genre_text}"
            set_text_frame(TCON, genre_text)
        else:
            set_text_frame(TCON, "Podcast")

        if metadata.get('copyright'): set_text_frame(TCOP, metadata['copyright'])
        if metadata.get('date'): set_text_frame(TDRC, metadata['date'])
        elif metadata.get('year'): set_text_frame(TDRC, str(metadata['year']))
        
        if metadata.get('track'):
            track_str = str(metadata['track'])
            if metadata.get('total_tracks'): track_str = f"{metadata['track']}/{metadata['total_tracks']}"
            set_text_frame(TRCK, track_str)
        
        if metadata.get('disc'):
            disc_str = str(metadata['disc'])
            if metadata.get('total_seasons'): disc_str = f"{metadata['disc']}/{metadata['total_seasons']}"
            set_text_frame(TPOS, disc_str)

        web_url = metadata.get('website') if metadata.get('website') else metadata.get('url')
        if web_url:
            audio.tags.delall('WOAR')
            audio.tags.add(WOAR(url=web_url))

        full_desc = metadata.get('description', '')
        if full_desc:
            if metadata.get('rating'): full_desc = f"{metadata['rating']}\n\n{full_desc}"
            audio.tags.delall('COMM')
            audio.tags.add(COMM(encoding=3, lang='eng', desc='', text=full_desc))

        audio.tags.add(TXXX(encoding=3, desc='PCST', text='1'))
        audio.tags.delall('TCMP')
        audio.tags.add(TCMP(encoding=3, text='1'))
        audio.tags.add(TXXX(encoding=3, desc='ITUNESMEDIATYPE', text='Podcast'))
        
        if metadata.get('content_rating'): audio.tags.add(TXXX(encoding=3, desc='ITUNESADVISORY', text=metadata['content_rating']))
        if metadata.get('frequency'): audio.tags.add(TXXX(encoding=3, desc='Release Frequency', text=metadata['frequency']))
        if metadata.get('total_tracks'): audio.tags.add(TXXX(encoding=3, desc='Total Episodes', text=str(metadata['total_tracks'])))
        if metadata.get('disc'): audio.tags.add(TXXX(encoding=3, desc='Season', text=str(metadata['disc'])))
        if metadata.get('track'): audio.tags.add(TXXX(encoding=3, desc='Episode', text=str(metadata['track'])))
        if artist: audio.tags.add(TXXX(encoding=3, desc='Host', text=artist))

        if metadata.get('image_data'):
            audio.tags.delall('APIC')
            audio.tags.add(APIC(encoding=3, mime='image/jpeg', type=3, desc='Cover', data=metadata['image_data']))

        audio.save(v2_version=4)
        print("[-] Tags added.")
        print("-" * 50)

    except Exception:
        print("[!] Error tagging file.")

def normalize_string(s):
    if not s: return ""
    s = s.replace('’', "'").replace('“', '"').replace('”', '"').replace('–', '-').replace('—', '-')
    return re.sub(r'[\W_]+', '', s).lower()

def fetch_rss_items(feed_url, apple_meta, podcast_id):
    print("[-] Accessing episode list...")
    try:
        response = requests.get(feed_url, headers=HEADERS)
        response.encoding = 'utf-8'
        for prefix, uri in NAMESPACES.items():
            ET.register_namespace(prefix, uri)
            
        root = ET.fromstring(response.content)
        channel = root.find('channel')
        
        # Global Metadata
        global_author = channel.find('itunes:author', NAMESPACES)
        global_author_text = global_author.text if global_author is not None else ""
        rss_copyright = channel.find('copyright')
        rss_copyright_text = rss_copyright.text if rss_copyright is not None else ""
        final_copyright = apple_meta.get('copyright') if apple_meta.get('copyright') else rss_copyright_text

        global_image = channel.find('itunes:image', NAMESPACES)
        global_image_url = global_image.get('href') if global_image is not None else None
        
        global_image_data = None
        if global_image_url:
            try:
                print("[-] Fetching cover art...")
                img_req = requests.get(global_image_url, headers=HEADERS)
                if img_req.status_code == 200: global_image_data = img_req.content
            except: pass

        items = channel.findall('item') if channel is not None else root.findall('item')
        parsed_items = []
        
        for item in items:
            title_tag = item.find('title')
            enclosure = item.find('enclosure')
            
            if title_tag is not None and enclosure is not None:
                title = title_tag.text
                media_url = enclosure.get('url')
                
                item_author = item.find('itunes:author', NAMESPACES)
                author = item_author.text if item_author is not None else global_author_text
                
                episode_num = item.find('itunes:episode', NAMESPACES)
                episode_val = episode_num.text if episode_num is not None else None
                season_num = item.find('itunes:season', NAMESPACES)
                season_val = season_num.text if season_num is not None else None

                summary_tag = item.find('itunes:summary', NAMESPACES)
                desc_tag = item.find('description')
                description = ""
                if summary_tag is not None and summary_tag.text: description = summary_tag.text
                elif desc_tag is not None and desc_tag.text: description = desc_tag.text
                else: description = apple_meta.get('description', '')
                
                if description: description = re.sub('<[^<]+?>', '', description)

                pub_date_tag = item.find('pubDate')
                full_date_string = None
                year = None
                if pub_date_tag is not None:
                    try:
                        dt = parsedate_to_datetime(pub_date_tag.text)
                        full_date_string = f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
                        year = str(dt.year)
                    except: pass

           
                parsed_url = urlparse(media_url)
                ext = os.path.splitext(parsed_url.path)[1]
                if not ext: ext = ".mp3"
                if "?" in ext: ext = ext.split("?")[0]
                
                safe_title_fn = title
                if len(safe_title_fn) > 150: safe_title_fn = safe_title_fn[:150]
                filename_prefix = f"{episode_val} - " if episode_val else ""
                filename = f"{filename_prefix}{safe_title_fn}{ext}"

                meta_dict = {
                    'title': title,
                    'album': apple_meta.get('title'),
                    'author': author,
                    'genre': apple_meta.get('genres'),
                    'rating': apple_meta.get('rating'),
                    'url': apple_meta.get('url'),
                    'website': apple_meta.get('website'),
                    'feed_url': feed_url,
                    'podcast_id': podcast_id,
                    'copyright': final_copyright,
                    'description': description,
                    'date': full_date_string,
                    'year': year,
                    'track': episode_val,
                    'disc': season_val,
                    'total_tracks': len(items),
                    'image_data': global_image_data,
                    'content_rating': apple_meta.get('content_rating'),
                    'frequency': apple_meta.get('frequency'),
                    'host': apple_meta.get('host'),
                    'media_url': media_url,
                    'filename': filename
                }
                parsed_items.append(meta_dict)
        
        return parsed_items

    except ET.ParseError:
        print("[!] Error: Failed to process podcast list.")
        return []
    except Exception as e:
        print(f"[!] Error: {e}")
        return []

def main():
    parser = argparse.ArgumentParser(description="Apple Podcast Downloader")
    parser.add_argument("url", help="The Apple Podcast URL")
    args = parser.parse_args()
    url = args.url

    show_id, episode_id = get_ids_from_url(url)
    if not show_id:
        print("[!] Error: Invalid URL.")
        sys.exit(1)
        
    apple_meta, jsonld_summary = scrape_apple_metadata(url)
    

    is_single_episode_mode = bool(episode_id)
    target_episode_title = None

    if is_single_episode_mode:
        print("[-] Detected Single Episode Mode.")
        target_episode_title, feed_url, show_title = get_episode_details(episode_id)
        if not feed_url:
            feed_url, show_title = get_show_details(show_id)
            if apple_meta.get('title'): target_episode_title = apple_meta.get('title')
        
        if not target_episode_title:
            print("[!] Error: Could not identify episode title.")
            sys.exit(1)
    else:
        print("[-] Detected Series Mode.")
        feed_url, show_title = get_show_details(show_id)

    if not feed_url:
        print("[!] Error: Could not find media source.")
        sys.exit(1)

    show_title = show_title or "Unknown Podcast"
    safe_title = re.sub(r'[\\/*?:"<>|]', "", show_title)
    if not os.path.exists(safe_title): os.makedirs(safe_title)

    items = fetch_rss_items(feed_url, apple_meta, show_id)
    if not items:
        sys.exit(1)

    download_queue = []

    if is_single_episode_mode:
      
        norm_target = normalize_string(target_episode_title)
        for item in items:
            norm_title = normalize_string(item['title'])
            if norm_title == norm_target or (norm_target in norm_title) or (norm_title in norm_target):
                download_queue.append(item)
                break
        if not download_queue:
            print(f"[!] Error: Episode '{target_episode_title}' not found in feed.")
    
    else:

        save_summary_file(safe_title, show_title, jsonld_summary, apple_meta)
        
        print(f"[-] Found {len(items)} episodes.")
        action = questionary.select(
            "What would you like to do?",
            choices=[
                "Download All Episodes",
                "Select Specific Episode(s)",
                "Exit"
            ]
        ).ask()

        if action == "Exit":
            sys.exit(0)
        elif action == "Download All Episodes":
            download_queue = items
        else:
            choices = []
            for item in items:
                choices.append(questionary.Choice(item['title'], value=item))
            
            selected_items = questionary.checkbox(
                "Select episodes to download (Space to select, Enter to confirm):",
                choices=choices
            ).ask()
            
            if selected_items:
                download_queue = selected_items
            else:
                print("[-] No episodes selected.")
                sys.exit(0)

    print(f"[-] Queued {len(download_queue)} files for download.")
    for item in download_queue:
        saved_path = download_file(item['media_url'], safe_title, item['filename'])
        if saved_path and saved_path.lower().endswith('.mp3'):
            add_tags(saved_path, item)

    print("[-] Operation complete.")

if __name__ == "__main__":
    main()
