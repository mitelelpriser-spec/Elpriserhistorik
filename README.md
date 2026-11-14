# elpris-data

Færdige prisfiler til appen MitEl – Elpriser. Et lille script (GitHub Actions) henter spotpriser fra Energi Data Service
hver time og lægger dem som én fil pr. måned og prisområde (kvartersopløsning, UTC). Appen henter filerne via GitHub Pages.

## Opsætning (én gang)
1. Opret et **offentligt** repo på GitHub, fx `elpris-data`, og læg indholdet af denne mappe i det (inkl. `.github`).
2. Repo → Settings → Pages → Source: *Deploy from a branch* → Branch `main`, mappe `/ (root)`.
3. Repo → Actions → *Opdater priser* → *Run workflow* (første kørsel henter 14 måneder).
4. Din adresse bliver: `https://BRUGERNAVN.github.io/REPONAVN/data/` – åbn `.../data/index.json` for at se, at den virker.
5. Sæt adressen i appens `Config.kt` → `HISTORY_BASE_URL` og byg appen igen.

Hold øje med, at repoet ikke går helt stille i 60 dage (GitHub kan slå planlagte kørsler fra); scriptet committer jævnligt nye data, så det holder normalt.
