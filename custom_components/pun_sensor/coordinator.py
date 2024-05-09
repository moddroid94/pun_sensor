"""Coordinator for pun_sensor"""

# pylint: disable=W0613
from datetime import date, datetime, timedelta
import io
import logging
from statistics import mean
import zipfile
from zoneinfo import ZoneInfo

from aiohttp import ClientSession, ServerConnectionError

import holidays

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later, async_track_point_in_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
import homeassistant.util.dt as dt_util

from .const import (
    CONF_ACTUAL_DATA_ONLY,
    CONF_SCAN_HOUR,
    COORD_EVENT,
    DOMAIN,
    EVENT_UPDATE_FASCIA,
    EVENT_UPDATE_PUN,
    PUN_FASCIA_F1,
    PUN_FASCIA_F2,
    PUN_FASCIA_F3,
    PUN_FASCIA_F23,
    PUN_FASCIA_MONO,
)
from .utils import get_fascia, get_next_date, extract_xml

_LOGGER = logging.getLogger(__name__)

tz_pun = ZoneInfo("Europe/Rome")


class PUNDataUpdateCoordinator(DataUpdateCoordinator):
    """Data coordinator"""

    session: ClientSession

    def __init__(self, hass: HomeAssistant, config: ConfigEntry) -> None:
        """Gestione dell'aggiornamento da Home Assistant"""
        super().__init__(
            hass,
            _LOGGER,
            # Nome dei dati (a fini di log)
            name=DOMAIN,
            # Nessun update_interval (aggiornamento automatico disattivato)
        )

        # Salva la sessione client e la configurazione
        self.session = async_get_clientsession(hass)

        # Inizializza i valori di configurazione (dalle opzioni o dalla configurazione iniziale)
        self.actual_data_only = config.options.get(
            CONF_ACTUAL_DATA_ONLY, config.data[CONF_ACTUAL_DATA_ONLY]
        )
        self.scan_hour = config.options.get(CONF_SCAN_HOUR, config.data[CONF_SCAN_HOUR])

        # Inizializza i valori di default
        self.web_retries = 0
        self.schedule_token = None
        self.pun = [0.0, 0.0, 0.0, 0.0, 0.0]
        self.orari = [0, 0, 0, 0, 0]
        self.fascia_corrente: int | None = None
        self.fascia_successiva: int | None = None
        self.prossimo_cambio_fascia: datetime | None = None
        self.termine_prossima_fascia: datetime | None = None

        _LOGGER.debug(
            "Coordinator inizializzato (con 'usa dati reali' = %s).",
            self.actual_data_only,
        )

    def clean_tokens(self):
        """Clear schedule tokens, if any."""
        # Annulla eventuali schedulazioni attive
        if self.schedule_token is not None:
            self.schedule_token()
            self.schedule_token = None

    async def _async_update_data(self):
        """Aggiornamento dati a intervalli prestabiliti"""

        # Calcola l'intervallo di date per il mese corrente
        date_end = dt_util.now().date()
        date_start = date(date_end.year, date_end.month, 1)

        # All'inizio del mese, aggiunge i valori del mese precedente
        # a meno che CONF_ACTUAL_DATA_ONLY non sia impostato
        if (not self.actual_data_only) and (date_end.day < 4):
            date_start = date_start - timedelta(days=3)

        start_date_param = str(date_start).replace("-", "")
        end_date_param = str(date_end).replace("-", "")

        # URL del sito Mercato elettrico
        download_url = f"https://gme.mercatoelettrico.org/DesktopModules/GmeDownload/API/ExcelDownload/downloadzipfile?DataInizio={start_date_param}&DataFine={end_date_param}&Date={end_date_param}&Mercato=MGP&Settore=Prezzi&FiltroDate=InizioFine"

        # imposta gli header della richiesta
        heads = {
            "moduleid": "12103",
            "referrer": "https://gme.mercatoelettrico.org/en-us/Home/Results/Electricity/MGP/Download?valore=Prezzi",
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": "Windows",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "tabid": "1749",
            "userid": "-1",
        }

        # Effettua il download dello ZIP con i file XML
        _LOGGER.debug("Inizio download file ZIP con XML.")
        async with self.session.get(download_url, headers=heads) as response:
            # aspetta la request
            bytes_response = await response.read()

            # se la richiesta NON e' andata a buon fine ritorna l'errore subito
            if response.status != 200:
                _LOGGER.error("Request Failed with code %s", response.status)
                raise ServerConnectionError(
                    f"Request failed with error {response.status}"
                )

            # la richiesta e' andata a buon fine, tenta l'estrazione
            try:
                archive = zipfile.ZipFile(io.BytesIO(bytes_response), "r")

            # Esce perché l'output non è uno ZIP, o ha un errore IO
            except (zipfile.BadZipfile, OSError) as e:  # not a zip:
                _LOGGER.error(
                    "Error failed download. url %s, length %s, response %s",
                    download_url,
                    response.content_length,
                    response.status,
                )
                raise UpdateFailed("Archivio ZIP scaricato dal sito non valido.") from e

        # Mostra i file nell'archivio
        _LOGGER.debug(
            "%s file trovati nell'archivio (%s)",
            len(archive.namelist()),
            ", ".join(str(fn) for fn in archive.namelist()),
        )
        # Estrae i dati dall'archivio
        extracted_data = extract_xml(archive)

        # Salva i risultati nel coordinator
        self.orari[PUN_FASCIA_MONO] = len(extracted_data[PUN_FASCIA_MONO])
        self.orari[PUN_FASCIA_F1] = len(extracted_data[PUN_FASCIA_F1])
        self.orari[PUN_FASCIA_F2] = len(extracted_data[PUN_FASCIA_F2])
        self.orari[PUN_FASCIA_F3] = len(extracted_data[PUN_FASCIA_F3])
        # iter on orari
        for i in range(5):  # stop is omitted
            if self.orari[i] > 0:
                self.pun[i] = mean(extracted_data[i])
            # fascia F23
            if i == 4:
                # Calcola la fascia F23 (a partire da F2 ed F3)
                # NOTA: la motivazione del calcolo è oscura ma sembra corretta; vedere:
                # https://github.com/virtualdj/pun_sensor/issues/24#issuecomment-1829846806
                if (self.orari[PUN_FASCIA_F2] and self.orari[PUN_FASCIA_F3]) > 0:
                    # Esistono dati sia per F2 che per F3
                    self.orari[PUN_FASCIA_F23] = (
                        self.orari[PUN_FASCIA_F2] + self.orari[PUN_FASCIA_F3]
                    )
                    self.pun[PUN_FASCIA_F23] = (
                        0.46 * self.pun[PUN_FASCIA_F2] + 0.54 * self.pun[PUN_FASCIA_F3]
                    )
                else:
                    # Devono esserci dati sia per F2 che per F3 affinché il risultato sia valido
                    self.orari[PUN_FASCIA_F23] = 0
                    self.pun[PUN_FASCIA_F23] = 0

        # Logga i dati
        _LOGGER.debug("Numero di dati: " + ", ".join(str(i) for i in self.orari))
        _LOGGER.debug("Valori PUN: " + ", ".join(str(f) for f in self.pun))
        return

    async def update_fascia(self, now=None):
        """Aggiorna la fascia oraria corrente"""

        # Scrive l'ora corrente (a scopi di debug)
        _LOGGER.debug(
            "Ora corrente sistema: %s",
            dt_util.now().strftime("%a %d/%m/%Y %H:%M:%S %z"),
        )
        _LOGGER.debug(
            "Ora corrente fuso orario italiano: %s",
            dt_util.now(time_zone=tz_pun).strftime("%a %d/%m/%Y %H:%M:%S %z"),
        )

        # Ottiene la fascia oraria corrente e il prossimo aggiornamento
        self.fascia_corrente, self.prossimo_cambio_fascia = get_fascia(
            dt_util.now(time_zone=tz_pun)
        )

        # Calcola la fascia futura ri-applicando lo stesso algoritmo
        self.fascia_successiva, self.termine_prossima_fascia = get_fascia(
            self.prossimo_cambio_fascia
        )

        _LOGGER.info(
            "Nuova fascia corrente: F%s (prossima: F%s)",
            self.fascia_corrente,
            self.fascia_successiva,
            self.prossimo_cambio_fascia.strftime("%a %d/%m/%Y %H:%M:%S %z"),
        )

        # Notifica che i dati sono stati aggiornati (fascia)
        self.async_set_updated_data({COORD_EVENT: EVENT_UPDATE_FASCIA})

        # Schedula la prossima esecuzione
        async_track_point_in_time(
            self.hass, self.update_fascia, self.prossimo_cambio_fascia
        )

    async def update_pun(self, now=None):
        """Aggiorna i prezzi PUN da Internet (funziona solo se schedulata)"""
        # Aggiorna i dati da web
        try:
            # Esegue l'aggiornamento
            await self._async_update_data()

            # Se non ci sono eccezioni, ha avuto successo
            self.web_retries = 0
        # errore nel fetch dei dati
        except ServerConnectionError as e:
            # Errori durante l'esecuzione dell'aggiornamento, riprova dopo
            if self.web_retries < 6:
                # exponential retry time using retry number, max 25min. after 5 try
                self.web_retries = +1
                retry_in_minutes = self.web_retries * self.web_retries
            else:
                # Sesto errore, tentativi esauriti
                self.web_retries = 0

                # Schedula al giorno dopo
                retry_in_minutes = 0

            # Annulla eventuali schedulazioni attive
            self.clean_tokens()

            # Prepara la schedulazione
            if retry_in_minutes > 0:
                # Minuti dopo
                _LOGGER.warn(
                    "Errore durante l'aggiornamento via web, nuovo tentativo tra %s minut%s.",
                    retry_in_minutes,
                    "o" if retry_in_minutes == 1 else "i",
                    exc_info=e,
                )
                self.schedule_token = async_call_later(
                    self.hass, timedelta(minutes=retry_in_minutes), self.update_pun
                )
            else:
                # Giorno dopo
                _LOGGER.error(
                    "Errore durante l'aggiornamento via web, tentativi esauriti.",
                    exc_info=e,
                )
                next_update_pun = get_next_date(
                    dt_util.now(time_zone=tz_pun), self.scan_hour, 1
                )
                self.schedule_token = async_track_point_in_time(
                    self.hass, self.update_pun, next_update_pun
                )
                _LOGGER.debug(
                    "Prossimo aggiornamento web: %s",
                    next_update_pun.strftime("%d/%m/%Y %H:%M:%S %z"),
                )
            # Esce e attende la prossima schedulazione
            return

        # pylint: disable=W0718
        # Broad Except catching
        # possibili errori: estrazione dei dati, file non zip.
        # Non ha avuto errori nel download, da gestire diversamente, per ora schedula a domani
        # #TODO Wrap XML extracion into try/catch to re-raise into something we can expect
        except (Exception, UpdateFailed) as e:
            # Giorno dopo
            # Annulla eventuali schedulazioni attive
            self.clean_tokens()

            _LOGGER.error(
                "Errore durante l'estrazione dei dati",
                exc_info=e,
            )

            next_update_pun = get_next_date(
                dt_util.now(time_zone=tz_pun), self.scan_hour, 1
            )

            self.schedule_token = async_track_point_in_time(
                self.hass, self.update_pun, next_update_pun
            )

            _LOGGER.debug(
                "Prossimo aggiornamento web: %s",
                next_update_pun.strftime("%d/%m/%Y %H:%M:%S %z"),
            )
            # Esce e attende la prossima schedulazione
            return
        # Notifica che i dati PUN sono stati aggiornati con successo
        self.async_set_updated_data({COORD_EVENT: EVENT_UPDATE_PUN})

        # Calcola la data della prossima esecuzione
        next_update_pun = get_next_date(dt_util.now(), self.scan_hour)
        if next_update_pun <= dt_util.now():
            # Se l'evento è già trascorso la esegue domani alla stessa ora
            next_update_pun = next_update_pun + timedelta(days=1)

        # Annulla eventuali schedulazioni attive
        self.clean_tokens()

        # Schedula la prossima esecuzione
        self.schedule_token = async_track_point_in_time(
            self.hass, self.update_pun, next_update_pun
        )
        _LOGGER.debug(
            "Prossimo aggiornamento web: %s",
            next_update_pun.strftime("%d/%m/%Y %H:%M:%S %z"),
        )
