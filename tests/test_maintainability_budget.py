from scripts import check_maintainability_budget as budget


def test_analyzer_detects_all_debt_classes_with_stable_ids():
    long_body = "\n".join(f"    value_{index} = {index}" for index in range(82))
    source = (
        "import sys\n"
        "sys.path.insert(0, 'x')\n"
        "def risky():\n"
        "    try:\n"
        "        return 1\n"
        "    except Exception:\n"
        "        return 0\n"
        "def large():\n"
        f"{long_body}\n"
    )
    first = budget.analyze_source("skills/example.py", source)
    second = budget.analyze_source("skills/example.py", source)
    assert {item["kind"] for item in first} == {
        "sys_path_mutation",
        "broad_exception",
        "long_function",
    }
    assert [item["id"] for item in first] == [item["id"] for item in second]


def test_repository_baseline_and_waiver_files_exist():
    assert budget.BASELINE_FILE.is_file()
    assert budget.WAIVERS_FILE.is_file()
