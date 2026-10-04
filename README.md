# Kalloori — R.M.D. Engineering College Website Rebuild

Repository containing the complete structural website archive, information architecture inventory, and offline HTML snapshot of **R.M.D. Engineering College** (`https://www.rmd.ac.in/`).

---

## Overview

- **Target Domain:** `https://www.rmd.ac.in/`
- **Scope:** Complete Phase 1 Structural Crawl & Information Architecture discovery.
- **HTML Pages Preserved:** 593 reachable internal web pages across 46 departments, centers, and administrative sections.
- **Local Styling:** Downloaded CSS stylesheets, JavaScript plugins, web fonts, and UI navigation graphics to render accurate offline previews.
- **Heavy Media Handling:** 832 PDFs and large photo archives are cataloged in `PHASE-2-ASSETS.csv` for selective download during Phase 2.

---

## Repository Structure

```
.
├── index.html                        # Website Homepage (served at root)
├── netlify.toml                      # Netlify deployment configuration
├── README.md                         # Project documentation
├── .gitignore                        # Git ignore rules
├── css/                              # Bootstrap stylesheets and themes
├── js/                               # JavaScript libraries and plugins
├── dept/                             # 46 Academic Departments & Cells
│   ├── ece/                          # ECE (index.html, faculty.html, lab.html, login.html)
│   ├── cse/                          # CSE
│   └── ...
├── images/                           # Brand identity and navigation graphics
├── newimages/                        # Icons and section assets
├── aboutus/, academics/, ...         # Static content sections and facilities
└── rmd-site-crawl/                   # Crawler engine, audit database & reports
    ├── crawl.py                      # Master resumable crawler (SQLite-backed)
    ├── crawl_departments.py          # Recursive department subpage crawler
    ├── download_styling.py           # CSS, JS, fonts, and UI graphics downloader
    ├── verify_and_download_missing.py# Parity audit and missing page resolver
    ├── README.md                     # Detailed crawler documentation & manual
    ├── PHASE-2-ASSETS.csv            # 2,351 internal heavy assets ready for Phase 2
    ├── data/
    │   ├── crawl.db                  # SQLite database with all crawl state
    │   ├── pages.csv                 # Page inventory (canonical URLs, titles, SHA-256)
    │   ├── links.csv                 # Discovered internal & external links with anchors
    │   ├── external-links.csv        # Catalog of external domains and portals
    │   ├── assets.csv                # Complete asset inventory
    │   ├── errors.csv                # Catalog of live server errors (404s, 403s)
    │   └── redirects.csv             # URL redirect mappings
    └── reports/
        ├── crawl-summary.json        # 15-point executive crawl summary report
        ├── site-tree.txt             # Complete navigation and URL hierarchy tree
        └── broken-links.txt          # Dead links on live server and referencing pages
```

---

## Key Features

1. **Root-Level Static Hosting Ready:**  
   `index.html` and all 593 pages are structured at root, ready to deploy instantly on Netlify (`https://kalloori.netlify.app`), GitHub Pages, Vercel, or Apache/Nginx.
2. **Deterministic Local Mapping:**  
   Original HTML is preserved byte-for-byte and mapped cleanly to matching relative file paths (`dept/cse/index.html`, `dept/ece/faculty.html`, etc.).
3. **Offline Preview:**  
   Opening `index.html` in any browser renders the full homepage with responsive Bootstrap styling, dropdowns, and branding.
4. **Audit of Live Site Broken Links:**  
   `rmd-site-crawl/reports/broken-links.txt` indexes all 167 dead links on the existing server (e.g. typos, deleted faculty files) to ensure they are cleaned up in the rebuild.
