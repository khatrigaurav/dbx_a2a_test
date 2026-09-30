# Databricks notebook source
# A dead-simple notebook: prints who triggered it and a timestamp.
import datetime

try:
    user = spark.sql("SELECT current_user() AS u").collect()[0]["u"]
except Exception as e:
    user = f"(could not resolve: {e})"

print("=== WS A notebook triggered ===")
print("run_as identity:", user)
print("timestamp:", datetime.datetime.utcnow().isoformat(), "UTC")
print("Hello from the Workspace A notebook — cross-workspace trigger succeeded.")
