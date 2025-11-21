from dotenv import load_dotenv
load_dotenv()

import requests
from twilio.rest import Client
import os
## FUTURE UPDATE
# from flask import *

# app = Flask(__name__)


# @app.route("/sms", methods=['POST'])
# def sms_reply():
#     """Respond to incoming SMS messages"""
#     incoming_msg = request.form.get('Body', '').strip().upper()
#     resp = MessagingResponse()
    
#     if not incoming_msg:
#         resp.message("Please send a stock ticker symbol (e.g., TSLA, AAPL, GOOGL)")
#         return str(resp)


STOCK_NAME = input("stock ticker: ")
your_no = os.environ.get("YOUR_NO")
twilio_no =os.environ.get("TWILIO_NO")
auth_token =os.environ.get("AUTH_TOKEN")
account_sid = os.environ.get("ACCOUNT_SID")
news_api = os.environ.get("NEWS_API_KEY")
stock_api = os.environ.get("STOCK_API_KEY")

STOCK_ENDPOINT = "https://www.alphavantage.co/query"
NEWS_ENDPOINT = "https://newsapi.org/v2/everything"

stock_params ={
    "function":"TIME_SERIES_DAILY",
    "symbol": STOCK_NAME,
    "apikey": stock_api,
}
news_params ={
    "q": STOCK_NAME,
    "apikey": news_api,
}


response = requests.get(STOCK_ENDPOINT,params=stock_params)

response_json = response.json()["Time Series (Daily)"]
data_list = [value for (key,value) in response_json.items()]
yesterday_data = data_list[0]
yesterday_data_closing_price = yesterday_data["4. close"]
day_before_yesterday_data = data_list[1]
day_before_yesterday_closing_price = day_before_yesterday_data["4. close"]
difference =   float(yesterday_data_closing_price) - float(day_before_yesterday_closing_price)
diff_percentage = (difference/ float(day_before_yesterday_closing_price)) * 100


if diff_percentage > 5:
    response_news = requests.get(NEWS_ENDPOINT,params=news_params)
    response_news_json = response_news.json()
    articles = response_news_json.get("articles",[])
    latestnews = articles[0:3]

    formatted_articles =[]
    for article in latestnews:
        title = article.get("title","No title")
        description = article.get("description","No description")
        formatted_articles.append(f"Title:{title}\nDescription:{description}\n")
        message_body = f"{STOCK_NAME}: {'🔺' if difference > 0 else '🔻'}{abs(diff_percentage):.2f}%\n\n"
        message_body += "\n".join(formatted_articles)

        client = Client(account_sid,auth_token)
        message = client.messages.create(
            body=message_body,
            to=your_no,
            from_=twilio_no,
        )