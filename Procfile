# Single worker: brochure PDF enrichment (UNION Box / Drive) is memory-heavy;
# each gunicorn worker has its own RSS, so --workers >1 multiplies peak RAM.
web: gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --timeout 960 --max-requests 20 --max-requests-jitter 5
