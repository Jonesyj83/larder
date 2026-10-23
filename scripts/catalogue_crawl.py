"""Shared walk-the-whole-catalogue logic, used by the Coles, Woolworths and
Aldi and IGA crawlers. Each store's own script just supplies its category list,
its own fetch function, and which tables/id column to write into -- the
resume-on-failure, backoff and append-only history bits are identical
across the stores so they live here once."""
import time


def crawl(db, *, catalogue_table, history_table, progress_table, id_field,
          categories, fetch, delay=12.0, page_size=48, sleep=None, log=print, max_retries=2):
    sleep = sleep or time.sleep
    progress = {r["category"]: r for r in db.execute(
        f"SELECT category, next_page, done FROM {progress_table}").fetchall()}
    total_written = 0
    requested = False
    try:
        for category in categories:
            state = progress.get(category)
            if state and state["done"]:
                continue
            page = state["next_page"] if state else 1
            misses = 0
            while True:
                if requested:
                    sleep(delay)
                requested = True
                try:
                    products, total = fetch(category, page)
                except Exception as exc:
                    misses += 1
                    # A soft block doesn't clear mid-run no matter how long
                    # this one run backs off for -- better to save the spot
                    # and come back next time than burn every remaining
                    # category trying the same wall and getting nothing.
                    log(f"{category} page {page} failed: {type(exc).__name__}: {exc}")
                    if misses >= max_retries:
                        log(f"blocked for now -- picking back up at {category} page {page} next run")
                        db.execute(f"""INSERT INTO {progress_table}(category, next_page, done) VALUES(?,?,0)
                            ON CONFLICT(category) DO UPDATE SET next_page=excluded.next_page, done=0""",
                            (category, page))
                        db.commit()
                        return {"categories": len(categories), "products": total_written, "blocked": True}
                    sleep(delay * (3 ** misses))
                    continue
                misses = 0
                if not products:
                    db.execute(f"""INSERT INTO {progress_table}(category, next_page, done) VALUES(?,?,1)
                        ON CONFLICT(category) DO UPDATE SET next_page=excluded.next_page, done=1""", (category, page))
                    db.commit()
                    break
                for p in products:
                    db.execute(f"""INSERT INTO {catalogue_table}
                            ({id_field}, name, brand, size, price, was_price, unit_price, unit_measure, category, updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))
                        ON CONFLICT({id_field}) DO UPDATE SET
                            name=excluded.name, brand=excluded.brand, size=excluded.size,
                            price=excluded.price, was_price=excluded.was_price, unit_price=excluded.unit_price,
                            unit_measure=excluded.unit_measure, category=excluded.category, updated_at=excluded.updated_at""",
                        (p[id_field], p["name"], p["brand"], p["size"], p["price"], p["was_price"],
                         p["unit_price"], p["unit_measure"], category))
                    # Append-only -- this is what the price-history graph and
                    # fake-special checks actually read from. The row above
                    # only ever holds the latest snapshot for fast matching.
                    db.execute(f"""INSERT INTO {history_table}
                            ({id_field}, price, was_price, unit_price, unit_measure, checked_at)
                        VALUES (?,?,?,?,?,datetime('now'))""",
                        (p[id_field], p["price"], p["was_price"], p["unit_price"], p["unit_measure"]))
                    total_written += 1
                finished = page * page_size >= total or len(products) < 10
                db.execute(f"""INSERT INTO {progress_table}(category, next_page, done) VALUES(?,?,?)
                    ON CONFLICT(category) DO UPDATE SET next_page=excluded.next_page, done=excluded.done""",
                    (category, page + 1, int(finished)))
                db.commit()
                log(f"{category} page {page}: {len(products)} products ({total} total in category)")
                if finished:
                    break
                page += 1
        return {"categories": len(categories), "products": total_written, "blocked": False}
    finally:
        pass
