"""
Generate score jobs and schedules.
"""

import pandas as pd
from dagster import (
    get_dagster_logger,
    job,
    op,
    ScheduleDefinition,
    JobDefinition,
    DefaultScheduleStatus,
)
from anomstack.config import specs
from anomstack.df.save import save_df
from anomstack.jinja.render import render
from anomstack.sql.read import read_sql
from anomstack.io.load import load_model
from anomstack.fn.run import define_fn


def build_score_job(spec) -> JobDefinition:
    """
    Build job definitions for score jobs.

    Args:
        spec (dict): A dictionary containing specifications for the job.

    Returns:
        JobDefinition: A job definition for the score job.
    """

    logger = get_dagster_logger()

    metric_batch = spec["metric_batch"]
    model_path = spec["model_path"]
    table_key = spec["table_key"]
    db = spec["db"]
    preprocess_params = spec["preprocess_params"]

    @job(name=f"{metric_batch}_score")
    def _job():
        """
        Get data for scoring and score data.
        """

        @op(name=f"{metric_batch}_get_score_data")
        def get_score_data() -> pd.DataFrame:
            """
            Get data for scoring.

            Returns:
                pd.DataFrame: A pandas dataframe containing data for scoring.
            """

            df = read_sql(render("score_sql", spec), db)

            return df

        @op(name=f"{metric_batch}_score_op")
        def score(df) -> pd.DataFrame:
            """
            Score data.

            Args:
                df (pd.DataFrame): A pandas dataframe containing data to be scored.

            Returns:
                pd.DataFrame: A pandas dataframe containing the scored data.
            """

            preprocess = define_fn(
                fn_name="preprocess", fn=render("preprocess_fn", spec)
            )

            df_scores = pd.DataFrame()

            for metric_name in df["metric_name"].unique():

                df_metric = df[df["metric_name"] == metric_name]

                model = load_model(metric_name, model_path, metric_batch)

                X = preprocess(df_metric, **preprocess_params)

                scores = model.predict_proba(X)

                # create initial df_score
                df_score = pd.DataFrame(
                    data=scores[:, 1],  # probability of anomaly
                    index=X.index,
                    columns=["metric_value"]
                )

                # limit to timestamps where metric_score is null to begin with in df_metric
                df_score = df_score[
                    df_score.index.isin(
                        df_metric[df_metric["metric_score"].isnull()]['metric_timestamp']
                    )
                ].reset_index()

                # merge some df_metric info onto df_score
                df_score = df_score.merge(
                    df_metric[["metric_timestamp", "metric_name", "metric_batch"]],
                    on=["metric_timestamp"],
                )
                df_score["metric_type"] = "score"
                df_score = df_score[
                    [
                        "metric_timestamp",
                        "metric_name",
                        "metric_value",
                        "metric_batch",
                        "metric_type",
                    ]
                ]

                df_scores = pd.concat([df_scores, df_score], ignore_index=True)

            logger.info(df_scores)

            return df_scores

        @op(name=f"{metric_batch}_save_scores")
        def save_scores(df) -> pd.DataFrame:
            """
            Save scores to db.

            Args:
                df (pd.DataFrame): A pandas dataframe containing the scored data.

            Returns:
                pd.DataFrame: A pandas dataframe containing the saved data.
            """

            df = save_df(df, db, table_key)

            return df

        save_scores(score(get_score_data()))

    return _job


# Build score jobs and schedules.
score_jobs = []
score_schedules = []
for spec_name, spec in specs.items():
    score_job = build_score_job(spec)
    score_jobs.append(score_job)
    if spec["score_default_schedule_status"] == "RUNNING":
        score_default_schedule_status = DefaultScheduleStatus.RUNNING
    else:
        score_default_schedule_status = DefaultScheduleStatus.STOPPED
    score_schedule = ScheduleDefinition(
        job=score_job,
        cron_schedule=spec["score_cron_schedule"],
        default_status=score_default_schedule_status,
    )
    score_schedules.append(score_schedule)
