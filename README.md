# Mutual Fund FAQ Assistant

> **Facts-only. No investment advice.**

A facts-only RAG chatbot that answers objective questions about 19 mutual fund schemes, cites exactly one source per answer, and refuses advisory questions. Full setup, architecture overview and limitations are completed in Phase 12; design details live in [docs/architecture.md](docs/architecture.md).

## Selected AMCs and schemes

*Draft note (Phase 1); the full scheme table is added in Phase 12.*

The problem statement asks for one AMC, 3-5 schemes, and official sources only (AMC, AMFI, SEBI). This project deliberately departs from those lines:

- **Groww scheme pages are cited for scheme answers.** The 19 Groww pages supplied in the problem statement are both the source of scheme facts and the single citation link for scheme questions and performance questions. Groww is an aggregator, so its figures may lag AMC factsheets, KIMs and SIDs. The "Last updated from sources" date shows when the page was last fetched.
- **19 schemes across 8 AMCs** (HDFC, Aditya Birla Sun Life, Nippon India, ICICI Prudential, PPFAS, Bandhan, Kotak, Edelweiss) are covered, because that is the reference list provided.
- **General, statement-download and refusal answers still cite official pages:** AMFI (Mutual Funds Sahi Hai, amfiindia.com), SEBI investor pages, and the CAMS statement pages.
- **Published performance figures are stated as facts.** Returns, NAV, AUM, category averages, Groww's ranks, risk ratios and holdings are extracted and can be quoted when asked, always with their basis (annualised/absolute/SIP) and as-of date. The assistant never judges, ranks or recommends funds, and never computes figures of its own; questions like "is this fund performing well?" or "which has the lower expense ratio?" get a refusal with the Groww link. It can put 2-4 schemes' *published* figures side by side ("compare the expense ratio, NAV and 3Y CAGR of Kotak Multi Cap and ICICI Prudential Large Cap"): a table read straight from the facts tables, unsorted, with no verdict, citing each scheme's page. That reply type has one link per scheme instead of exactly one ([docs/architecture.md §6.3a](docs/architecture.md)). This reads the problem statement's "no performance comparisons or return calculations" and "link to the factsheet only" lines as forbidding interpretation and computation, not restating published data. See [docs/architecture.md §2.4](docs/architecture.md).

The decision is recorded in `config/settings.yaml` as `citation_policy: groww`. The corpus review is in [docs/corpus-review.md](docs/corpus-review.md).
