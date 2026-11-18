# Manual downloads from hdfcfund.com

hdfcfund.com blocks automated requests, so these documents are downloaded by hand in a browser. The ingest pipeline does not try to get around that block.

For each row:

1. Open hdfcfund.com in your browser and find the document.
2. Save it in this folder with the exact file name below.
3. Copy the URL of the page or PDF you downloaded it from, and paste it into the matching `url: ""` line in [../sources.yaml](../sources.yaml). Every citation shown to users points to this URL, so it must be the real address.

| Source id | Save as | What to download |
|---|---|---|
| hdfc_large_cap_kim | `hdfc_large_cap_kim.pdf` | Key Information Memorandum (KIM) for HDFC Large Cap Fund |
| hdfc_flexi_cap_kim | `hdfc_flexi_cap_kim.pdf` | KIM for HDFC Flexi Cap Fund |
| hdfc_elss_kim | `hdfc_elss_tax_saver_kim.pdf` | KIM for HDFC ELSS Tax Saver |
| hdfc_mid_cap_kim | `hdfc_mid_cap_kim.pdf` | KIM for HDFC Mid Cap Fund |
| hdfc_liquid_kim | `hdfc_liquid_kim.pdf` | KIM for HDFC Liquid Fund |
| hdfc_factsheet | `hdfc_factsheet.pdf` | The latest monthly factsheet (one PDF covering all schemes) |
| hdfc_ter | `hdfc_ter.xls` | The total expense ratio (TER) spreadsheet download from the TER disclosure page (an .xlsx workbook, despite the name). Only the latest date for each scheme is ingested |
| hdfc_statements | `hdfc_statements.html` | The page explaining how to get account and capital-gains statements, saved with the browser's "Save Page As", HTML only |

If a KIM isn't available, the full Scheme Information Document (SID) works too. Change `doc_type: kim` to `doc_type: sid` for that entry.

The file's date on disk becomes the "Last checked" date shown with every answer from it, so download fresh copies when refreshing the corpus.

Then, from `backend/`:

```bash
uv run python -m corpus.ingest
uv run python ../evals/run_retrieval.py
uv run python ../evals/run_facts.py
```
