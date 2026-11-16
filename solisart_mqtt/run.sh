#!/usr/bin/with-contenv bashio
set -e

export SOLISART_LOGIN="$(bashio::config 'login')"
export SOLISART_PASSWORD="$(bashio::config 'password')"
export SOLISART_ID="$(bashio::config 'installation_id')"
export SOLISART_INTERVAL="$(bashio::config 'interval')"
export SOLISART_CODES="$(bashio::config 'codes')"
export SOLISART_RUNTIME_KEYS="$(bashio::config 'runtime_keys')"
export LOG_LEVEL="$(bashio::config 'log_level')"

# /data est le seul volume persistant d'un add-on : sessions et dictionnaire
# y survivent aux redémarrages et aux mises à jour.
export SOLISART_CACHE="/data/cache"
mkdir -p "${SOLISART_CACHE}"

# Renommage optionnel des grandeurs : /data/names.json
# {"flT1": "Température capteurs", "rlV3": "Vanne 3 voies"}
if [ -f /data/names.json ]; then
    export SOLISART_NAMES="/data/names.json"
fi

bashio::log.info "Démarrage de la passerelle SolisArt"
exec python3 /app/solisart_mqtt.py
