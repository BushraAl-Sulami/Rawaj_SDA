import json
import os


# Load existing Research Agent result
with open(
    "results/dearduck_sa_research_20260916_202211.json",
    "r",
    encoding="utf-8"
) as f:
    research_result = json.load(f)


qualification_input = {
    # Restaurant information
    "restaurant": research_result.get("restaurant", {}),

    # Instagram profile information
    "profile": research_result.get("profile", {}),

    # Analysis of the profile
    "profile_analysis": research_result.get("profile_analysis", {}),

    # Calculated research metrics
    "metrics": research_result.get("metrics", {}),

    # Research signals
    "research_signals": research_result.get("research_signals", [])
}


os.makedirs("outputs", exist_ok=True)

with open(
    "outputs/casamyrrariyadh_qualification_input.json",
    "w",
    encoding="utf-8"
) as f:
    json.dump(
        qualification_input,
        f,
        indent=4,
        ensure_ascii=False
    )


print("Qualification input saved successfully.")