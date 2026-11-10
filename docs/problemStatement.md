# Problem Statement: Mutual Fund FAQ Assistant (Facts-Only Q&A)

## Overview

Build a facts-only FAQ assistant for mutual fund schemes, using **Groww** as the reference product context. The assistant answers objective, verifiable queries by retrieving information **exclusively from official public sources** (AMC websites, AMFI, SEBI).

It must never give investment advice, opinions, or recommendations. Every response carries a single, clear source link and follows defined constraints on clarity, accuracy, and compliance.

## Objective

Design and implement a lightweight **Retrieval-Augmented Generation (RAG)** assistant that:

- Answers factual queries about mutual fund schemes
- Uses a curated corpus of official documents
- Provides concise, source-backed responses

## Target Users

- Retail investors comparing mutual fund schemes
- Customer support and content teams handling repetitive mutual fund queries

## Scope of Work

### 1. Corpus Definition

- Select **one** Asset Management Company (AMC)
- Choose **3–5** schemes with category diversity (e.g., large-cap, flexi-cap, ELSS)
- Collect **15–25** official public URLs, including:
  - Scheme factsheets
  - KIM (Key Information Memorandum)
  - SID (Scheme Information Document)
  - AMC FAQ/help pages
  - AMFI/SEBI guidance pages
  - Statement and tax document download guides
- Reference pages (Groww) used as the source material to build the project:
  - https://groww.in/mutual-funds/hdfc-equity-fund-direct-growth
  - https://groww.in/mutual-funds/birla-sun-life-equity-fund-direct-growth
  - https://groww.in/mutual-funds/aditya-birla-sun-life-large-cap-direct-fund-growth
  - https://groww.in/mutual-funds/aditya-birla-sun-life-multi-cap-fund-direct-growth
  - https://groww.in/mutual-funds/birla-sun-life-small-midcap-fund-direct-growth
  - https://groww.in/mutual-funds/aditya-birla-sun-life-bse-total-market-index-fund-direct-growth
  - https://groww.in/mutual-funds/birla-sun-life-corporate-bond-fund-direct-growth
  - https://groww.in/mutual-funds/birla-sun-life-short-term-fund-direct-growth
  - https://groww.in/mutual-funds/nippon-india-large-cap-fund-direct-growth
  - https://groww.in/mutual-funds/icici-prudential-large-cap-fund-direct-growth
  - https://groww.in/mutual-funds/hdfc-silver-etf-fof-direct-growth
  - https://groww.in/mutual-funds/hdfc-pharma-and-healthcare-fund-direct-growth
  - https://groww.in/mutual-funds/hdfc-nifty-next-50-index-fund-direct-growth
  - https://groww.in/mutual-funds/hdfc-medium-term-opportunities-fund-direct-growth
  - https://groww.in/mutual-funds/parag-parikh-long-term-value-fund-direct-growth
  - https://groww.in/mutual-funds/bandhan-small-cap-fund-direct-growth
  - https://groww.in/mutual-funds/icici-prudential-corporate-bond-fund-direct-plan-growth
  - https://groww.in/mutual-funds/kotak-multi-cap-fund-direct-growth
  - https://groww.in/mutual-funds/edelweiss-mid-and-small-cap-fund-direct-growth

### 2. FAQ Assistant Requirements

Answer facts-only queries such as:

- Expense ratio of a scheme
- Exit load details
- Minimum SIP amount
- ELSS lock-in period
- Riskometer classification
- Benchmark index
- Process to download statements or capital gains reports

Every response must:

- Be **at most 3 sentences**
- Include **exactly one** citation link
- End with a footer: `Last updated from sources: <date>`

### 3. Refusal Handling

Refuse non-factual or advisory queries, e.g. "Should I invest in this fund?" or "Which fund is better?". Refusals must:

- Be polite and clearly worded
- Reinforce the facts-only limitation
- Provide a relevant educational link (e.g., AMFI or SEBI resource)

### 4. User Interface (Minimal)

- A welcome message
- Three example questions
- A visible disclaimer: **"Facts-only. No investment advice."**

## Constraints

### Data and Sources

- Use only official public sources (AMC, AMFI, SEBI)
- No third-party blogs or aggregator websites

### Privacy and Security

Do not collect, store, or process:

- PAN or Aadhaar numbers
- Account numbers
- OTPs
- Email addresses or phone numbers

### Content Restrictions

- No investment advice or recommendations
- No performance comparisons or return calculations
- For performance-related queries, link to the official factsheet only

### Transparency

- Responses must be short, factual, and verifiable
- Every answer includes a source link and last-updated date

## Expected Deliverables

1. **README document**
   - Setup instructions
   - Selected AMC and schemes
   - Architecture overview (RAG approach)
   - Known limitations
2. **Disclaimer snippet:** "Facts-only. No investment advice."
3. **Multiple chat thread support:** a RAG chatbot that handles multiple independent conversations/threads simultaneously

## Success Criteria

- Accurate retrieval of factual mutual fund information
- Strict adherence to facts-only responses
- Consistent inclusion of valid source citations
- Proper refusal of advisory queries
- Clean, minimal, user-friendly interface

## Summary

Build a trustworthy, transparent, and compliant mutual fund FAQ assistant that prioritizes **accuracy over intelligence**. Users should receive only verified, source-backed financial information, with no advisory bias or speculative content.
