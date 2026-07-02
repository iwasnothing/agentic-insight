---
name: workflow-skill
description: Plan research actions, analyze accumulated evidence, and evaluate audit quality.
---

# Workflow Orchestration Skill

Use this skill when orchestrating the overall multi-step audit loop, including action planning, comprehensive report analysis, and quality evaluation.

## Action Planning Rules
- Generate an action plan listing steps to retrieve necessary data to address the objective and resolve gaps.
- The plan steps MUST use ONLY the tools: 'doc_context_retrieval' and 'tabular_data_retrieval'. Do not use any other tools.
- DO NOT use, query, reference, or retrieve any data from the table 'loan_prediction_train'. It is completely excluded.
- DO NOT request steps or query strings that select all data or retrieve the entire dataset in general. Each step must use a specific query with a specific objective (e.g. matching specific filters, columns, or metrics).

## Auditing Analysis & Recommendations
- Perform a comprehensive financial database analysis based on the objective, accumulated context, guidelines, and retrieved data.
- Provide clear, actionable recommendations formatted in Markdown.

## Quality QA Evaluation
- Compare the generated analysis against the objective.
- Rate your confidence in the completeness of the analysis on a scale of 0 to 100.
- Provide a detailed explanation justifying the score and listing any remaining gaps/uncertainties.
