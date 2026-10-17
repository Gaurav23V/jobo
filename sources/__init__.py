"""Job sources: each one yields normalized ``JobCandidate`` objects.

The seam is deliberately small (see docs/sources.md):

  * ``sources.base.JobCandidate`` - what every source produces.
  * ``sources.store.insert_candidates`` - the ONE insert path: cross-source
    dedup, then insert. Every source goes through it.
  * ``sources.urls`` - URL normalization shared by every source.
  * ``policy.job_filters`` - the rule filters (collection time and post-enrich).

Current sources: the LinkedIn alert-email collector (module1) and jobfound.org
(sources/jobfound.py).
"""
