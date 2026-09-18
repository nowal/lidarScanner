# Sample operations lead emails

Three lead emails as operations would receive them, for review of the
format and the ranked provider section. Each is here as the plain-text
part (`.txt`, with the subject line on top) and the HTML part (`.html`);
a real send carries both.

| File | Scenario | What to look at |
|---|---|---|
| `ops-email-urban.*` | Painting, zip 37203. Seven candidates: a partner, a four-platform business, a past quoter with weak reviews, a five-star business on three reviews, a link-only prospect, a web-search lead | Every relationship type in one list; how the score explains itself per row |
| `ops-email-rural.*` | Power washing, zip 37033. Two candidates: one with seven Google reviews, one with nothing on file | The small-set note; nothing invented for the company with no data |
| `ops-email-conflicting.*` | Window cleaning, zip 37203. A four-time past quoter rated 3.4, a stranger rated 4.9 on 388 reviews, a company with big social followings and no reviews anywhere, a partner with nothing on file | The **PAST QUOTER** mark against a better-reviewed stranger; followers shown as followers, never as reviews |

**Everything in these files is fictional.** Provider rows come from the
test fixture (`tests/fixtures/provider_candidates.json`), not from the demo
seed or any real partner list; the businesses, ratings, counts, and links
do not exist. Homeowners are stand-ins with 555 numbers and example.com
addresses. The only location is the zip. The 3D model link and the entry
link point at placeholder hosts.

Rules the format follows (design record: `docs/adr/provider-ranking.md`):

- The street address is never in the email; it is released to operations
  only after the homeowner selects a quote.
- No conversation thread id, homeowner id, or processor job id appears.
  Operations addresses everything by the quote request id.
- Ratings and review counts come from Google Places only. Yelp, Facebook,
  Instagram and Nextdoor are profile links; a follower count appears only
  when one was recorded from a stated source. A missing number is shown as
  missing, never as zero.
- "TakeShape partner" is used only for rows TakeShape entered as partners.
- A company that has returned a quote through the system is marked
  **PAST QUOTER ×N** and gets the separately listed "quoted boost".

Regenerate after a rendering change with:

```
.venv/Scripts/python scripts/ops_email_samples.py
```

The delivery path in the deployed environment (transport, public base URL,
the send queue) is tracked separately; these files show composition only.
