# Remote PlusPlus

Miglioramenti di NVDA Remote con funzionalità di produttività per utenti esperti.

## Funzionalità

### Gestore delle connessioni

Salva, organizza e connettiti rapidamente alle sessioni remote utilizzate di frequente.

* Organizza le connessioni in gruppi
* Cerca le connessioni per nome o host
* Connettiti con un click dall'elenco
* Copia negli appunti un link alla connessione

### Scambio modalità di controllo

Passa istantaneamente dal ruolo di controllore a quello di controllato o viceversa, senza disconnettersi.

### Connessione al server predefinito

Connettiti rapidamente al server di connessione automatica configurato con un gesto.

## Gestore delle connessioni

Il gestore delle connessioni fornisce una pratica interfaccia per la gestione delle connessioni remote..

### Lista combinazioni da tastiera

| Azione | Combinazione |
|--------|----------|
| Connetti in modalità inversa | `Shift+invio` |
| Sposta la connessione verso l'alto | `Alt+freccia su` |
| Sposta la connessione verso il basso | `Alt+freccia giù` |
| Modifica la connessione | `F2` |
| Elimina la connessione | `Delete` |
| Seleziona tutto | `Ctrl+a` |
| Copia link | `Ctrl+c` |

### Menu contestuale

Fare clic con il tasto destro su una connessione per accedere ad opzioni aggiuntive:

* Connetti / connetti in modalità inversa
* Modifica
* Copia link
* Imposta come connessione automatica
* Sposta in alto/sposta in basso
* Elimina

## Combinazioni da tastiera

| Azione | combinazione |
|---------|---------|
| Apri il gestore connessioni | `NVDA+Control+Shift+N` |
| Modalità inversa | `NVDA+Control+Shift+W` |
| Connetti al server di default | Non assegnata |

## Elementi del menu

Tutte le funzionalità sono inoltre accessibili dal menu accesso remoto di NVDA:

* Gestore connessioni...
* Scambia modalità di controllo
* Connetti al server di default (visualizzata solo quando è configurata la connessione automatica)

## Rewuisiti

* NVDA 2026.1 o versione successiva con la funzionalità accesso remoto integrato abilitata

## Sicurezza

Per garantire la massima sicurezza e impedire accessi involontari, questo componente aggiuntivo è disabilitato sul desktop protetto (ad esempio, richieste UAC, schermata di accesso di Windows).

## Autore

Cary-rowen <cary-rowen@outlook.com>

## Audio a bassa latenza

L'audio a bassa latenza usa `NVDARemoteAudioServer` per trasmettere l'audio del
computer controllato. Vengono usati automaticamente l'host della connessione
Remote corrente, la porta fissa `6838` e la chiave Remote esistente. L'audio è
disattivato quando si crea una connessione e non ha impostazioni nell'editor.

Nel menu Remote Access sono disponibili due opzioni indipendenti:

* Ascolta i suoni del dispositivo di uscita predefinito di Windows sul sistema remoto
* Ascolta il microfono remoto

Le opzioni sono disponibili solo sul computer che controlla. La selezione chiede
al computer controllato di avviare l'acquisizione; le due sorgenti possono essere
attive insieme e vengono miscelate. Il computer controllato deve avere Remote++
e il modulo audio incluso. Con un vecchio client Remote, senza Remote++ o senza
modulo audio, l'audio viene segnalato come non disponibile o va in timeout, mentre la
connessione Remote resta attiva. Non viene assegnata una scorciatoia audio.

In Impostazioni NVDA → Remote++ si possono scegliere il buffer di riproduzione
(minimo, predefinito; 10, 20, 40 o 80 ms), il bitrate totale (64, 96 predefinito,
192 kbps), i canali (mono o stereo predefinito) e la modalità di trasmissione.
La modalità a bassa latenza usa pacchetti da 10 ms (predefinita); quella bilanciata
usa 20 ms e riduce il sovraccarico dei pacchetti, aggiungendo attesa.
La frequenza di campionamento è fissa a 48 kHz. Il buffer minimo non significa latenza zero.
Le preferenze sono globali: Applica riavvia brevemente
l'ascolto attivo mantenendo le sorgenti selezionate, senza attivare l'audio spento.
Entrambi i computer devono avere Remote++ con supporto Opus; non si torna al vecchio
formato PCM. Il normale controllo remoto e la sintesi vocale continuano a funzionare.

L'audio viene compresso all'interno di NVDA con la DLL x64 libopus inclusa nel componente,
senza installare altri ambienti di esecuzione. L'audio decodificato viene riprodotto in blocchi da 5 ms. Il protocollo
audio non è cifrato: usare una rete fidata o una VPN.
