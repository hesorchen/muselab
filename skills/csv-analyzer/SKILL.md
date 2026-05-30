---
name: csv-analyzer
description: USE WHEN a user shares or references a CSV file and wants it summarized, analyzed, or visualized — produces data overview, summary statistics, missing-data report, and relevant charts using built-in tools (Read + Bash running Python/pandas).
---

# CSV Analyzer

This Skill analyzes CSV files and provides comprehensive summaries with statistical insights and visualizations, using your built-in tools — no bundled scripts required.

## When to Use This Skill

Use this Skill whenever the user:
- Uploads or references a CSV file
- Asks to summarize, analyze, or visualize tabular data
- Requests insights from CSV data
- Wants to understand data structure and quality

## How It Works

### Default behavior

By default, run the analysis directly — don't make the user pick from a menu of options first. Read the file, inspect its structure, and present a complete summary with relevant charts.

The one exception: if the data is genuinely ambiguous (e.g., unclear what a column means, multiple plausible date formats, or it's unclear whether a value is the metric of interest), ask a single short clarifying question, then proceed. This keeps the skill fast while staying consistent with muselab's "ask when ambiguous" stance.

### How to run the analysis

Use the built-in tools directly — there is no separate script to call:
- Use **Read** to peek at the first rows and understand the columns.
- Use **Bash** to run inline Python (pandas, and matplotlib/seaborn if available) for stats and charts.

A typical inline analysis looks like:

```bash
python3 - <<'PY'
import pandas as pd
df = pd.read_csv("data.csv")
print(df.shape)
print(df.dtypes)
print(df.describe(include="all"))
print(df.isna().sum())
PY
```

For charts, save figures to PNG files (e.g. `plt.savefig("trend.png")`) and reference them in your summary.

### Analysis steps

1. **Load and inspect** the CSV file into a pandas DataFrame
2. **Identify data structure** - column types, date columns, numeric columns, categories
3. **Determine relevant analyses** based on what's actually in the data:
   - **Sales/E-commerce data** (order dates, revenue, products): Time-series trends, revenue analysis, product performance
   - **Customer data** (demographics, segments, regions): Distribution analysis, segmentation, geographic patterns
   - **Financial data** (transactions, amounts, dates): Trend analysis, statistical summaries, correlations
   - **Operational data** (timestamps, metrics, status): Time-series, performance metrics, distributions
   - **Survey data** (categorical responses, ratings): Frequency analysis, cross-tabulations, distributions
   - **Generic tabular data**: Adapts based on column types found

4. **Only create visualizations that make sense** for the specific dataset:
   - Time-series plots only if a date/timestamp column exists
   - Correlation heatmaps only if multiple numeric columns exist
   - Category distributions only if categorical columns exist
   - Histograms for numeric distributions when relevant
   
5. **Generate comprehensive output** automatically including:
   - Data overview (rows, columns, types)
   - Key statistics and metrics relevant to the data type
   - Missing data analysis
   - Multiple relevant visualizations (only those that apply)
   - Actionable insights based on patterns found in THIS specific dataset
   
6. **Present everything** in one complete analysis - no follow-up questions

**Example adaptations:**
- Healthcare data with patient IDs → Focus on demographics, treatment patterns, temporal trends
- Inventory data with stock levels → Focus on quantity distributions, reorder patterns, SKU analysis  
- Web analytics with timestamps → Focus on traffic patterns, conversion metrics, time-of-day analysis
- Survey responses → Focus on response distributions, demographic breakdowns, sentiment patterns

### Behavior Guidelines

**Do:**
- Lead with the analysis itself rather than a menu of options
- Generate the relevant charts automatically
- Be thorough and complete in the first response
- Save charts to files and reference them in the summary

**Avoid:**
- Stalling with "What would you like me to do?" when the intent is clearly "analyze this"
- Listing options for the user to choose from instead of just analyzing
- Providing a partial analysis that forces a follow-up

**Ask first only when:** the data is genuinely ambiguous (unclear column meaning, ambiguous date format, or unclear target metric) — then ask one short question and proceed.

### Example Prompts

> "Here's `sales_data.csv`. Can you summarize this file?"

> "Analyze this customer data CSV and show me trends."

> "What insights can you find in `orders.csv`?"

### Example Output

**Dataset Overview**
- 5,000 rows × 8 columns  
- 3 numeric columns, 1 date column  

**Summary Statistics**
- Average order value: $58.2  
- Standard deviation: $12.4
- Missing values: 2% (100 cells)

**Insights**
- Sales show upward trend over time
- Peak activity in Q4
*(Attached: trend plot)*

## Dependencies

Uses your built-in Read and Bash tools. The inline Python relies on:

- `pandas` (required) — `pip install pandas`
- `matplotlib` / `seaborn` (optional, for charts) — `pip install matplotlib seaborn`

If a charting library is missing, still produce the statistical summary and note that charts were skipped.

## Notes

- Detects date columns by dtype and by name (columns containing 'date'/'time'); parse with `pd.to_datetime` when needed
- Handles missing data gracefully and reports it explicitly
- Produces a chart only when the data supports it — time-series needs a date column, correlation heatmaps need multiple numeric columns, etc.
- All numeric columns are included in the statistical summary

