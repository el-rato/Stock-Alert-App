# 📈 Stock Price Alert App

This project is a small Python application that monitors daily stock
price movements and sends SMS alerts when a stock shows a significant
change. It pulls price data from **Alpha Vantage**, fetches related news
from **NewsAPI**, and delivers alerts using **Twilio SMS**. The goal is
to receive quick updates whenever a stock moves sharply so you can react
faster.

------------------------------------------------------------------------

## 🚀 Features

-   Retrieves **daily stock prices** for any ticker symbol.
-   Computes the **percentage difference** between yesterday and the
    previous day.
-   If the movement exceeds **5%**, the app:
    -   Fetches the **top three related news articles**
    -   Sends a neatly formatted **SMS alert**
-   Uses a `.env` file for storing all API keys and credentials.
-   Includes a planned upgrade for a **Flask-based SMS webhook**.

------------------------------------------------------------------------

## 🧰 Built With

-   Python 3\
-   Requests\
-   Twilio REST API\
-   Alpha Vantage API\
-   NewsAPI\
-   python-dotenv

------------------------------------------------------------------------

## 📦 Installation

### 1. Clone the Repository

``` bash
git clone https://github.com/your-username/stock-alert-app.git
cd stock-alert-app
```

### 2. Install Dependencies

``` bash
pip install -r requirements.txt
```

Expected `requirements.txt`:

    requests
    twilio
    python-dotenv

------------------------------------------------------------------------

## 🔐 Environment Variables

Create a file named `.env` in the project root and add:

    YOUR_NO=+91XXXXXXXXXX
    TWILIO_NO=+1XXXXXXXXXX
    AUTH_TOKEN=your_twilio_auth_token
    ACCOUNT_SID=your_twilio_account_sid
    NEWS_API_KEY=your_newsapi_key
    STOCK_API_KEY=your_alpha_vantage_key

Make sure this file is **not committed** to GitHub.

------------------------------------------------------------------------

## ▶️ Running the App

Run the script:

``` bash
python main.py
```

You will be prompted to enter a stock ticker symbol:

    stock ticker: TSLA

If the stock price has moved more than 5%, you'll receive an SMS with:

-   Up/Down movement indicator (🔺 or 🔻)
-   Percentage change
-   Top 3 related news headlines and summaries

Example SMS:

    TSLA: 🔺6.12%

    Title: Tesla announces major update…
    Description: The company revealed…

------------------------------------------------------------------------

## 🧠 How It Works

1.  Requests daily price data from Alpha Vantage.\
2.  Extracts the last two closing prices.\
3.  Computes the percentage change.\
4.  If the change exceeds the threshold:
    -   Retrieves recent articles related to the stock.\
    -   Formats the information into a single message.\
    -   Sends the message through Twilio SMS.

------------------------------------------------------------------------

## 🛠 Planned Enhancements

-   Add a **Flask server** to process incoming SMS commands.
-   Allow users to subscribe to multiple tickers.
-   Add scheduled alerts for morning or evening summaries.
-   Create a simple dashboard for monitoring all subscribed stocks.

------------------------------------------------------------------------
