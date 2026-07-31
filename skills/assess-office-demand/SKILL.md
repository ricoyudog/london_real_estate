---
name: assess-office-demand
description: Assess free corporate office-demand and hybrid-working indicators for London office research. Use when an agent needs tenant-demand direction, Rightmove enquiry signals, ONS hybrid-working trends, or clear proxy limitations before drawing a market conclusion.
---

# Assess Office Demand

1. Read [corporate demand](../../wiki/research/datasource/09-corporate-office-demand.md) for the current Rightmove and business-demand sources.
2. Read [hybrid working](../../wiki/research/datasource/10-hybrid-working.md) when workplace attendance or hybrid behaviour matters.
3. Prefer a shipped Python function when the page lists one. Follow the documented browser steps for report-only sources.
4. Preserve the source period, geography, definition, URL, and retrieval date with every value.
5. Label Rightmove enquiries, surveys, and transport counts as `proxy`; never present them as signed take-up or physical office occupancy.
6. Keep Great Britain and London observations separate. Do not infer City, West End, Canary Wharf, Midtown, or Fringe values from national data.
7. For ONS hybrid data, compare the two returned observations and their confidence intervals; do not call a point-estimate change statistically meaningful when the intervals overlap.
8. Report the verified signal, its direction, and the known coverage gap before making any inference.
