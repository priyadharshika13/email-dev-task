"""
Context Processor: quarterly_planned_counts
------------------------------------------

Purpose:
    Provides aggregated counts of all *scheduled* email campaigns per financial
    quarter for the current year. This enables dashboards and analytics pages to
    quickly display upcoming workload distribution.

How It Works:
    - Fetches all Campaign objects with status `SCHEDULED`
    - Filters by current year (based on server timezone)
    - Groups the scheduled_time month into Q1, Q2, Q3, Q4 buckets

Returned Context:
    planned_quarter_counts = {
        "q1": <int>,   # Jan–Mar
        "q2": <int>,   # Apr–Jun
        "q3": <int>,   # Jul–Sep
        "q4": <int>,   # Oct–Dec
    }

Usage in Templates:
    {{ planned_quarter_counts.q1 }}
    {{ planned_quarter_counts.q2 }} etc.

Used For:
    - Admin dashboards
    - Quarterly email workload charts
    - Capacity planning reports

Assumptions:
    - Campaign model contains 'scheduled_time' datetime field
    - Campaign.Status.SCHEDULED is the enum for planned runs
"""

from django.utils import timezone
from .models import Campaign


def quarterly_planned_counts(request):
    """
    Compute scheduled email campaign counts by quarter for the current year.

    Args:
        request (HttpRequest): The incoming request object. Required by Django
        for all context processors but not used internally.

    Returns:
        dict: Dictionary containing:
            {
                "planned_quarter_counts": {
                    "q1": int,  # Jan–Mar
                    "q2": int,  # Apr–Jun
                    "q3": int,  # Jul–Sep
                    "q4": int,  # Oct–Dec
                }
            }

    Notes:
        - This logic should remain lightweight, as context processors execute
          on every template render.
        - Designed for dashboard visualizations and reporting.
    """
    now = timezone.now()
    year = now.year

    qs = Campaign.objects.filter(
        status=Campaign.Status.SCHEDULED,
        scheduled_time__year=year,
    )

    def count_between(start_month, end_month):
        return qs.filter(
            scheduled_time__month__gte=start_month,
            scheduled_time__month__lte=end_month,
        ).count()

    counts = {
        "q1": count_between(1, 3),
        "q2": count_between(4, 6),
        "q3": count_between(7, 9),
        "q4": count_between(10, 12),
    }

    return {"planned_quarter_counts": counts}
