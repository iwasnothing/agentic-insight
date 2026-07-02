---
name: tabular-retrieval-skill
description: Define data cubes, generate DuckDB SQL, and analyze tabular records.
---

# Tabular Retrieval Skill

Use this skill when you need to query or analyze tabular databases, generate DuckDB SQL queries, define OLAP data cubes, or analyze retrieved tabular datasets.

## Tabular Query Planning
- Decompose the user query into a plan consisting of highly specific SQL sub-queries.
- Each sub-query must target a particular metric, subset, or objective with clear parameters.
- DO NOT plan sub-queries that select all data or query the entire dataset in general.

## Data Cube Definition
- Define a Data Cube for a sub-query, detailing measures, dimensions, filtering conditions, sort order, and aggregation functions.

## DuckDB SQL Generation & Repair Rules (CRITICAL)
1. **Never reference the table 'loan_prediction_train'**: Do NOT use, query, or reference the table 'loan_prediction_train' or its columns (such as Loan_ID, Gender, Married, Dependents, Education, Self_Employed, ApplicantIncome, CoapplicantIncome, LoanAmount, Loan_Amount_Term, Credit_History, Property_Area, Loan_Status) under any circumstances.
2. **Only query the 'loan_data' table**: Only use the table 'loan_data' and columns explicitly listed in schemas. Do not assume or hallucinate columns (such as risk_category, insurance_status, guarantee_status, underwriter_notes).
3. **Avoid type mismatch in JOINs**: DO NOT compare a VARCHAR column (like text/string IDs) with numeric columns (like DOUBLE, BIGINT) in JOIN conditions or WHERE clauses. Ensure all compared/joined fields have identical data types.
4. **Double-quote column names containing dots**: Any column name containing dots (e.g. log.annual.inc, int.rate, credit.policy, days.with.cr.line, revol.bal, revol.util, inq.last.6mths, delinq.2yrs, pub.rec, not.fully.paid) MUST always be enclosed in double quotes in the SQL query (e.g. `"log.annual.inc"`, `"int.rate"`, `"credit.policy"`).
5. **Dotted alias naming**: SQL aliases / AS names must not contain dots unless they are double-quoted. Preferably use underscores (e.g., `AVG_log_annual_inc` instead of `AVG_log.annual.inc`).
6. **Group By rule**: If the query uses aggregates (SUM, AVG, COUNT, MIN, MAX), every non-aggregated column referenced in SELECT, ORDER BY, or WHERE clauses must appear in the GROUP BY clause.
7. **Always apply LIMIT and sorting**: Always generate SQL statements that cap the result to a maximum of the requested limit. Apply a LIMIT clause (e.g., `LIMIT 100`) and proper sorting order (using an `ORDER BY` clause) to ensure the most relevant rows are retrieved.
8. **Valid DuckDB SQL only**: Output strictly valid DuckDB SQL query inside the `sql_query` field. Do not include trailing braces/brackets like `}`, `]`, or `}]` inside the SQL query.

## Tabular Report Analysis
- Analyze retrieved tabular CSV data to answer the query.
- Provide the final answer and a detailed explanation in Markdown format.
