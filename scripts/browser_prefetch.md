# ORACLE Browser Pre-Fetch Task

Before running any Think Tank report, execute this browser workflow for each ticker.

## Step 1 - Connect to Chrome
/browser connect

## Step 2 - For each ticker, fetch transcript from Motley Fool

Navigate to: https://www.fool.com/earnings/call-transcripts/?symbol={TICKER}

If results appear:
- Click the most recent transcript link
- Wait for page to load
- Take a full snapshot
- Extract all text content from the transcript body
- Save to ~/ORACLE/cache/transcript_{TICKER}_{TODAY}.json

If no results:
- Try: https://seekingalpha.com/symbol/{TICKER}/earnings
- Click most recent earnings call transcript
- Extract text content
- Save to same cache location

## Step 3 - Fetch analyst consensus from StockAnalysis

Navigate to: https://stockanalysis.com/stocks/{ticker_lowercase}/forecast/

Extract:
- Analyst consensus price target (mean, high, low)
- Number of analysts
- Buy/Hold/Sell breakdown
- Most recent price target changes

Save to ~/ORACLE/cache/analyst_{TICKER}_{TODAY}.json

## Step 4 - Verify company identity on Yahoo Finance

Navigate to: https://finance.yahoo.com/quote/{TICKER}

Extract:
- Full company name from page header
- Current price
- 52-week high and low
- Market cap

Compare company name against ticker_names.json entry.
If mismatch: alert and do not proceed with Think Tank run.

## Step 5 - Disconnect browser
/browser disconnect
