#!/usr/bin/env bash

resolve_factorjoin_python() {
    local conda_bin="${CONDA_BIN:-}"
    local env_name="${FACTORJOIN_CONDA_ENV:-TestEnv}"
    local env_prefix=""

    if [[ -n "${FACTORJOIN_PYTHON:-}" ]]; then
        if [[ -x "$FACTORJOIN_PYTHON" ]]; then
            echo "$FACTORJOIN_PYTHON"
            return 0
        fi
        echo "ERROR: FACTORJOIN_PYTHON is not executable: $FACTORJOIN_PYTHON" >&2
        return 1
    fi

    if [[ -z "$conda_bin" ]]; then
        if command -v conda >/dev/null 2>&1; then
            conda_bin="$(command -v conda)"
        elif [[ -x /home/zxz/miniconda3/bin/conda ]]; then
            conda_bin=/home/zxz/miniconda3/bin/conda
        fi
    fi

    if [[ -n "$conda_bin" && -x "$conda_bin" ]]; then
        env_prefix="$("$conda_bin" env list | awk -v n="$env_name" '$1 == n {print $NF; exit}')"
        if [[ -n "$env_prefix" && -x "$env_prefix/bin/python" ]]; then
            echo "$env_prefix/bin/python"
            return 0
        fi
    fi

    echo "ERROR: FactorJoin conda env '$env_name' not found. Set FACTORJOIN_PYTHON or install the env." >&2
    return 1
}

run_factorjoin_python() {
    local python_bin
    python_bin="$(resolve_factorjoin_python)"
    env -u VIRTUAL_ENV -u PYTHONHOME -u PYTHONPATH -u PYTHONUSERBASE \
        -u CONDA_PREFIX -u CONDA_DEFAULT_ENV -u CONDA_SHLVL -u CONDA_PYTHON_EXE \
        PATH="$(dirname "$python_bin"):/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
        "$python_bin" "$@"
}
