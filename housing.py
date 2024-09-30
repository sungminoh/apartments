#! /usr/bin/env python
# -*- coding: utf-8 -*-
# vim:fenc=utf-8
#
# Copyright © 2019 smoh10 <smoh2044@gmail.com>
#
# Distributed under terms of the MIT license.

"""

"""
import inspect
from functools import lru_cache
from concurrent import futures
import logging
from pathlib import Path
import traceback
from typing import List, Literal, Optional, Union
from bs4 import BeautifulSoup
from bs4.element import Tag
import requests
from threading import Thread
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, splitquery
import webbrowser
import sys
import os
import json
import re
import googlemaps
from selenium import webdriver
from dataclasses import dataclass
import dotenv
import colorlog


dotenv.load_dotenv()
# Set up logger


def get_logger(
    name: str = None,
    level: int = logging.DEBUG,
    log_format: str = "%(asctime)s %(levelname)-5s %(lineno)4s:%(filename)-20s - %(message)s",
    parent: Optional[logging.Logger] = None,
) -> logging.Logger:
    """Get module logger

    Args:
        name (`str`, optional): The name of the logger
        logfile (`str`, default: 'dev/null'): Log file to write
        level (`int`, default: $LOGGER_LEVEL): Default value fallows
            environment variable.
        stream (`bool`, default: True): Print stdout if true
        log_format (`str`): Log format
        parent (`logging.Logger`): If this is given, create a child logger

    Returns:
        `logging.Logger`
    """
    prev_frame = inspect.currentframe().f_back  # type: ignore
    if not name:
        name = prev_frame.f_globals['__name__'] if prev_frame else ''
    logger = logging.getLogger(name) if parent is None else parent.getChild(name)
    logger.setLevel(level)
    color_formatter = colorlog.ColoredFormatter(
        '%(log_color)s' + log_format,
        datefmt=None,
        reset=True,
        style='%',
        log_colors={
            'DEBUG': 'cyan',
            'INFO': 'bold',
            'INFOV': 'cyan,bold',
            'WARNING': 'yellow',
            'ERROR': 'red,bold',
            'CRITICAL': 'red,bg_white',
        })
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(level)
    stream_handler.setFormatter(color_formatter)
    logger.addHandler(stream_handler)
    return logger


logger = get_logger()


@dataclass
class Post:
    yelp_review: str
    yelp_rating: str
    yelp_link: str
    google_review: str
    google_rating: str
    google_link: str
    price: str 
    title: str
    location: str
    link: str


@lru_cache(None)
def get_headers(url):
    ret = {}
    driver = webdriver.Chrome()
    driver.get(url)
    cookies = driver.get_cookies()
    # Convert cookies to a dictionary
    cookie_dict = {cookie['name']: cookie['value'] for cookie in cookies}
    # Update the HEADER_BASE with the new cookies
    cookie_string = '; '.join([f"{name}={value}" for name, value in cookie_dict.items()])
    ret['cookie'] = cookie_string
    s = driver.execute_script("var req = new XMLHttpRequest();req.open('GET', document.location, false);req.send(null);return req.getAllResponseHeaders()")
    for line in s.split("\r\n"):
        if line:
            key, value = line.split(': ', 1)
            ret[key.lower()] = value
    return ret


class Apartment:
    HEADER_BASE = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "accept-language": "en-US,en;q=0.9,ko;q=0.8",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    }

    def __init__(self, url):
        self.url = url
        self._pages = None
        self._headers = None
        self.memo = {}

    @property
    def headers(self):
        if self._headers is None:
            self._headers = {**self.HEADER_BASE, **get_headers(self.url)}
        return self._headers

    def _soup(self, page):
        url = self.url
        if page != 1:
            url, query = splitquery(self.url)
            url = f'{os.path.join(url, str(page))}/?{query}'
        response = requests.get(url, headers=self.headers)
        return BeautifulSoup(response.text, 'html.parser')

    def get_page(self, page):
        def get_price(p):
            price = p.find('p', 'property-pricing') \
                or p.find('span', 'property-rents') \
                or p.find('div', 'price-range')
            if price:
                price = price.text
            else:
                price = ''
            special = p.find('p', 'property-specials')
            if special:
                special = special.text.strip()
                price += ' ' + special
            return price

        def parse_property(p):
            title = (p.find('div', 'property-title') \
                or p.find('p', 'property-title')).text
            link = p.find('a', 'property-link').attrs['href']
            price = get_price(p)
            address = ', '.join([(x.text or '').strip() for x in p.find_all('div', 'property-address')])
            return Post(None, None, None, None, None, None, price, title, address, link)

        logger.debug(f"Getting page {page} from {self.url}")
        soup = self._soup(page)
        properties = soup.find_all("article", "placard")
        ret = []
        for p in properties:
            try:
                ret.append(parse_property(p))
            except Exception as e:
                logger.error(f"Error parsing property: {e}\n{traceback.format_exc()}")
                pass
        return ret

    @property
    def pages(self):
        if not self._pages:
            # <span class="pageRange">Page 3 of 28</span>
            pattern = r'Page (\d+) of (\d)+'
            pages = self._soup(1).find_all('span', attrs={'class': 'pageRange'})
            if not pages:
                self._pages = [1, 1]
            else:
                match = re.search(pattern, pages[0].contents[0])
                if match:
                    self._pages = [int(x) for x in match.groups()]
                else:
                    self._pages = [1, 1]
        return self._pages

    def get_list(self, page=None):
        pool = futures.ThreadPoolExecutor()
        results = [
            pool.submit(self.get_page, p)
            for p in range(self.pages[0], self.pages[1] + 1)
        ]
        ret = []
        for r in results:
            ret.extend(r.result())
        return ret


class Yelp:
    def __init__(self, query):
        self.query = query
        self._page_url = None
        self._headers = None
        self._review = [None, None]

    @property
    def headers(self):
        if self._headers is None:
            self._headers = {
                **get_headers("https://www.yelp.com"),
                **{
                    "accept": "*/*",
                    "accept-language": "en-US,en;q=0.9,ko;q=0.8",
                    "content-type": "application/json",
                    "origin": "https://www.yelp.com",
                },
            }
        return self._headers

    def _get_page_url_response(self, query):
        url = 'https://www.yelp.com/gql/batch'
        data = [
            {
                "operationName": "GetSuggestions",
                "variables": {
                    "capabilities": [],
                    "prefix": query,
                    "location": "San Francisco Bay Area, CA, United States",
                },
                "extensions": {
                    "operationType": "query",
                    "documentId": "109c8a7e92ee9b481268cf55e8e21cc8ce753f8bf6453ad42ca7c1652ea0535f",
                },
            }
        ]
        response = requests.post(url, headers=self.headers, json=data)
        return response 

    @property
    def page_url(self):
        if self._page_url is None:
            response = self._get_page_url_response(self.query)
            data = response.json()
            self._page_url = "Not found"
            if data:
                suggestions = data[0].get("data", {}).get("searchSuggestFrontend", {}).get(
                    "prefetchSuggestions", {}
                ).get("suggestions", {})
                if suggestions and suggestions[0].get("redirectUrl"):
                    self._page_url = "https://www.yelp.com" + suggestions[0].get(
                        "redirectUrl"
                    )
            logger.debug(f"Yelp url of {self.query!r} >>> {self._page_url}")
        return self._page_url

    def get_review_response(self):
        url = 'https://www.yelp.com/gql/batch'
        data = [{"operationName":"GetSuggestions","variables":{"capabilities":[],"prefix":self.query,"location":"San Francisco Bay Area, CA, United States"},"extensions":{"operationType":"query","documentId":"109c8a7e92ee9b481268cf55e8e21cc8ce753f8bf6453ad42ca7c1652ea0535f"}}]
        response = requests.post(url, headers=self.headers, json=data)
        return response 

    @property
    def review(self):
        if self._review[0] is None and self.page_url.startswith('http'):
            rating = None
            n_reviews = None

            resp = requests.get(self.page_url)
            soup = BeautifulSoup(resp.text, 'html.parser')
            review_cnt = soup.find('a', href='#reviews')
            if review_cnt:
                n_reviews = review_cnt.text
                parent = review_cnt.parent
                review_avg = parent.find_previous_sibling()
                rating = review_avg.text.strip()

            self._review = [n_reviews, rating]
        return self._review


class GoogleMap:
    def __init__(self, query):
        self.query = query
        self.url = None
        self._rating = [None, None]

    def get_place_result(self, fields: Optional[List[Literal['opening_hours', 'business_status', 'photos', 'price_level', 'plus_code', 'permanently_closed', 'icon']]] = None):
        fields = [
            "types",
            "place_id",
            "formatted_address",
            "name",
            "geometry",
            "rating",
            "user_ratings_total",
            *(fields or [])
        ]
        # Replace with your actual API key
        api_key = os.getenv("GOOGLE_MAPS_API_KEY")
        # Create a Google Maps client
        gmaps = googlemaps.Client(key=api_key)
        # Search for the place in the Bay Area
        query = self.query
        if "apartment" not in self.query.lower():
            query + " apartment in the bay area"
        results = gmaps.find_place(
            input=query,
            input_type="textquery",
            fields=fields,
            location_bias="circle:20000@37.7959572,-122.3944423",
        )
        return results

    @property
    def rating(self):
        if self._rating[0] is None:
            place_result = self.get_place_result()
            if place_result.get("candidates"):
                rating_avg = place_result["candidates"][0].get("rating")
                rating_cnt = place_result["candidates"][0].get("user_ratings_total")
                self.url = f'https://www.google.com/maps/place/?q=place_id:{place_result["candidates"][0]["place_id"]}'
                self._rating = [rating_cnt, rating_avg]
            else:
                logger.warning(f"No results found for {self.query}: {json.dumps(place_result)}")
        return self._rating


def crawl(url):
    def _get_yelp_result(post):
        logger.debug(f"Getting yelp result for {post.title}")
        yelp = Yelp(post.title)
        post.yelp_review, post.yelp_rating = yelp.review
        post.yelp_link = yelp.page_url
        return post

    def _get_google_result(post):
        logger.debug(f"Getting google result for {post.title}")
        google = GoogleMap(post.title)
        google_review, google_rating = google.rating
        google_link = google.url
        post.google_review, post.google_rating, post.google_link = (
            google_review,
            google_rating,
            google_link,
        )
        return post

    # posts = Apartment(url).get_list()
    posts = Apartment(url).get_page(1)[:3]
    # get yelp reviews
    # yelp_pool = futures.ThreadPoolExecutor(1)
    # yelp_futs = [yelp_pool.submit(_get_yelp_result, p) for p in posts]
    # get google reviews
    google_pool = futures.ThreadPoolExecutor()
    google_futs = [google_pool.submit(_get_google_result, p) for p in posts]

    # yelp_results = [fut.result() for fut in futures.as_completed(yelp_futs)]
    results = [fut.result() for fut in futures.as_completed(google_futs)]
    return results


def to_html(posts):
    ret = ['<table style="width:100%">',
           '''
           <tr>
            <th>yelp_rating</th>
            <th>yelp_review</th>
            <th>google_rating</th>
            <th>google_review</th>
            <th>price</th>
            <th>title</th>
            <th>location</th>
           </tr>
           ''']
    for post in posts:
        ret.append(f'''
                   <tr>
                   <td>{post.yelp_rating}</td>
                   <td><a href="{post.yelp_link}">{post.yelp_review}</a></td>
                   <td>{post.google_rating}</td>
                   <td><a href="{post.google_link}">{post.google_review}</a></td>
                   <td>{post.price}</td>
                   <td><a href="{post.link}">{post.title}</a></td>
                   <td>{post.location}</td>
                   <tr/>''')
    ret.append('</table>')
    return '\n'.join(ret)


if __name__ == '__main__':
    url = sys.argv[1]
    fname = sys.argv[2]
    with open(fname, 'w') as f:
        f.write(to_html(crawl(url)))
    webbrowser.open_new_tab('file://' + os.path.realpath(fname))
