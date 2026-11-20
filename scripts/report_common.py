"""Shared presentation helpers for the current benchmark reports."""
import html


def table(headers, rows, caption):
    esc = lambda value: html.escape(str(value), quote=True)
    return (
        '<div class="table-wrap"><table><caption class="sr-only">'
        + esc(caption)
        + "</caption><thead><tr>"
        + "".join(f'<th scope="col">{esc(h)}</th>' for h in headers)
        + "</tr></thead><tbody>"
        + "".join("<tr>" + "".join(f"<td>{esc(cell)}</td>" for cell in row) + "</tr>" for row in rows)
        + "</tbody></table></div>"
    )


def documentation_metadata(value):
    """Select operational metadata for reports while retaining raw evidence."""
    omitted = {
        "estimated_total_compute_usd", "estimated_compute_usd", "hourly_compute_usd",
        "usd_per_hour", "price_segments", "spot_history", "cost_scope",
        "estimate_notes", "estimated_seconds", "spot_price_cap", "result_cache",
    }
    if isinstance(value, dict):
        return {key: documentation_metadata(item) for key, item in value.items() if key not in omitted}
    if isinstance(value, list):
        return [documentation_metadata(item) for item in value]
    return value
