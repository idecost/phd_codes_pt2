# Scelte metodologiche del dataset

Le regole con cui il codice in `sources/` scarica, calcola e mette insieme i valori, fonte per fonte. Tra parentesi c'è la funzione o il file dove la scelta è scritta.

`test_event.ipynb` controlla il dataset ricostruendo i valori con queste regole a partire dai file originali. Se una regola cambia, va aggiornata qui e nella sezione 14 del notebook.

## Struttura e tempo

1. **Livelli**: solo admin0/1/2 ed event > episode > location > grid (`schema.LEVELS`).
2. **Formato long**: una riga per valore, con le colonne di luogo, evento, valore, dettaglio, tempo e origine (`schema.COLUMNS`). Solo la griglia è wide.
3. **Un campo vuoto è un dato mancante**: le righe senza numero né testo vengono tolte, e un dato assente non diventa mai zero (`schema.finish`).
4. **Date**: senza fuso, in UTC. Quelle illeggibili diventano NaT (`schema.to_utc`).
5. **Righe ripetute tra file**: le righe uguali in tutto tranne `source_file`/`retrieved_at` (download con finestre sovrapposte) si tengono una volta sola. Le righe ripetute nello stesso file restano (`build.drop_repeats`).
6. **`known_at` stimato**: se la fonte non dice quando un valore è stato pubblicato, si usa un limite superiore con `quality_flag=known_at_upper_bound`:
   - GDACS: `datemodified`;
   - GHSL: 31-12-2023 (release R2023A);
   - INFORM: 1° gennaio dell'anno di edizione.

   EM-DAT, DTM e HDX restano senza `known_at`.
7. **Fonti mai fuse**: se due fonti danno la stessa variabile, ognuna ha la sua colonna con il nome della fonte in fondo (es. `hazard_magnitude_gdacs` e `hazard_magnitude_emdat`) (`wide._named`).

## GDACS

8. **Cartella dell'evento**: ISO3 = `properties.iso3`, altrimenti il primo paese colpito; anno = anno di `fromdate`. Un evento su più paesi va nella cartella del primo (`extract.event_dir_from_eventdata`).
9. **Lista eventi**: chiede anche gli eventi Green (`alertlevel=green;orange;red`). Senza questo parametro l'API restituisce solo Orange e Red.
10. **Episodi**: quelli elencati in `properties.episodes[].details`, più `episodeid`, più ogni cartella già su disco.
11. **Record riscritti**: GDACS sovrascrive i suoi record, quindi su disco c'è sempre l'ultima versione. Hanno una vera data di disponibilità solo le versioni ShakeMap (`dateinserted`/`dateadded`) e gli impact run (`modelrun`).
12. **Coordinate delle ShakeMap**: nella stringa `conditions` GDACS scrive latitudine e longitudine invertite; il codice le scambia (`parse_conditions`).
13. **Data delle versioni ShakeMap**: la stessa versione ha due date, prese da file diversi:
    - magnitudo, profondità, MMI e coordinate (`event/gdacs_shakemap`) hanno `known_at = dateinserted` di `eventdata.json`;
    - allerta, impatto e bande (`details.json`) hanno `dateadded`.
14. **Data degli impact run**: `known_at` = le prime 14 cifre di `modelrun` (AAAAMMGGhhmmss), lette come UTC. Senza `modelrun` si usa `datemodified`, con il flag.
15. **Scalar degli impact run**: uno per **nome** in tutto il file; se un nome compare in due datasource si tiene il primo (`impact_scalars`).
16. **Validità**: tutte le righe GDACS hanno `valid_from/valid_to` = `fromdate/todate` dell'evento e `time_type=instant`.
17. **Numeri scritti come testo**: si prende il primo numero ("10 Km" → 10, "0.28 m" → 0.28) (`_as_number`). Un numero con la virgola delle migliaia verrebbe letto male.
18. **Esposizione del card**: "130 thousand (in MMI>=VII)" diventa 130.000 (`parse_exposed_population`). È un numero arrotondato e resta separato dal valore del modello (`PopulationOverVII`, 133.487 per EQ_1496100).
19. **Aree**:
    - ogni feature di `geometry.json` diventa `areas/<label>/area.geojson`, con label = `polygonlabel` (o `Class`) ripulito; i doppioni prendono `_1`, `_2`;
    - le bande Intensity sono **anelli** con buchi, non aree cumulative: `Intensity_4.5` è la fascia tra 4.5 e 5.
20. **Area delle bande**: calcolata su una sfera di raggio 6.378.137 m, buchi sottratti, con una formula approssimata: non è un'area ellissoidica esatta.
21. **Data delle aree**: `known_at` = `dateinserted` della versione ShakeMap del poligono, altrimenti `polygondate`, altrimenti `todate` dell'episodio.
22. **EMM**: solo le ultime `limit=50` notizie, quindi la prima data può cadere giorni dopo l'evento. Validità dalla prima all'ultima `pubdate` (`quality_flag=limited_to_latest_news`).
23. **EM-DAT**: validità = `fromDate`–`toDate` del primo record; nessun `known_at`.
24. **Tsunami**: `tsmaxheight` viene letto direttamente da `episodedata.json`, perché `build_variables` non lo legge.

## GHSL

25. **Quale raster**: la release più recente che contiene il prodotto, poi l'epoca più recente non successiva all'anno dell'evento (2025 → E2025), poi la versione più alta.
26. **Risoluzione**: il codice prende `min(r)`, cioè per POP la **più fine** (100 m, Mollweide 54009), anche se la docstring dice "coarsest". SMOD esiste solo a 1 km.
27. **Mai ricampionare il raster**: è il vettore a essere riproiettato sulla griglia del raster, così i valori dei pixel restano quelli originali (`to_raster_crs`).
28. **Geometrie**: `buffer(0)` su ognuna. Prima di estrarre, l'area viene fusa in un solo poligono (`dissolve`), per non contare due volte gli stessi pixel.
29. **Celle toccate**: tutte quelle con copertura > 0 (exactextract); i pixel nodata sono esclusi.
30. **`pop_ghsl`** (location): **somma non pesata** delle celle che toccano l'area.
31. **`pop_ghsl_covered`** (location): `sum` di exactextract, cioè la somma pesata per la frazione di cella coperta. Anche `cells_covered` è pesato, quindi non è un numero intero.
32. **SMOD è una classe**, mai una media:
    - `smod_majority` = classe di maggioranza pesata per copertura;
    - `frac_<c>` = quota d'area coperta da ogni classe;
    - `smod_share_rural/urban` (location) = quota dell'**area di terra** (classe 10 esclusa).
33. **Classi SMOD**: 10 acqua; 11, 12, 13 rurale; 21, 22, 23 e 30 urbano (`utils/smod.py`).
34. **POP × SMOD**: ogni cella da 100 m prende la classe della cella da 1 km che contiene il suo centro, con `floor(x/1000), floor(y/1000)`. Si assume che la griglia da 1 km sia allineata ai multipli di 1000 m (il notebook lo controlla). Si usano solo le celle con pop > 0; la classe −1 (cella SMOD non trovata) finisce in `quality_flag no_smod_cell_share`.
35. **Rurale e urbano nelle location**:
    - `pop_ghsl_rural` + `pop_ghsl_urban` non contano la classe 10 né la −1, quindi possono sommare meno di `pop_ghsl`;
    - `pop_share_rural` = rurale / totale, **con** acqua e −1 nel totale.
36. **Admin**:
    - `pop_ghsl` = somma non pesata delle celle che toccano l'unità. Una cella di confine conta per entrambe le unità vicine, quindi Σ admin2 ≥ admin1 ≥ admin0;
    - admin1 e admin0 sono l'unione delle celle senza ripetizioni, quindi sono esatti;
    - le quote rurale/urbano admin sono **conteggi di celle** di terra, mentre nelle location sono **quote d'area pesate**: due definizioni diverse;
    - `pop_share_rural` admin ha nel totale solo le classi SMOD, **senza** la −1 (nelle location la −1 è inclusa).
37. **Griglia**:
    - nella griglia da 100 m le celle con pop 0 vengono tolte, senza cambiare i totali;
    - c'è un file per episodio e risoluzione, con tutte le location insieme;
    - dove le aree si sovrappongono (cerchio 100km e bande) la stessa cella compare più volte con `location_id` diversi.
38. **Tempo GHSL**: validità = anno dell'epoca (`time_type=epoch`), `source_version=R2023A_E<epoch>`.

## IDMC

39. **Come si lega una cartella a un evento IDMC**:
    - prima per GLIDE vero (i codici AHA che sembrano GLIDE sono scartati, `true_glides`);
    - altrimenti con stesso paese, stesso tipo (EQ→Earthquake…), inizi a non più di 3 giorni e almeno una figura IDU entro 300 km dall'epicentro (confidence medium).
40. **Sequenze di scosse**: se un evento IDMC è legato a più cartelle, le sue cifre vanno a **una sola**: la scossa più grande, poi quella con GLIDE, poi la più vicina nel tempo. Le altre restano legate con `primary=False` e per loro lo spostamento è **mancante, non zero**. Flag `sequence_of_N_gdacs_events`.
41. **Eventi IDMC senza cartella**: hanno un id proprio, `<HAZ>_idmc-<id>`. "Storm" diventa ST, non TC.
42. **Regola IDU**:
    - per ogni evento si sommano le figure "Recommended figure"; se non ce ne sono, si somma l'ultima "Triangulation" di ogni luogo (per `created_at`);
    - flag `possible_overlap_admin0` se tra le righe usate c'è una figura nazionale;
    - IDU è provvisorio. Il dump mondiale viene scaricato intero, tenuto in cache 2 ore e filtrato in locale; per finestre che iniziano più di 170 giorni fa si usa il dump "all".
43. **GIDD**:
    - flussi = somma delle figure "Internal Displacements" (ID unico);
    - stock = somma delle figure "Total number of IDPs" dell'ultimo anno che ne ha;
    - flussi e stock non si sommano mai; GIDD e IDU stanno in colonne separate.
44. **GIDD per paese (admin0)**: il flusso vale per l'anno (1/1–31/12), lo stock al 31/12 (`valid_from = valid_to` = fine anno).
45. **Figure su più luoghi**: il valore viene **ripetuto** su ogni luogo, con `shared_by_N`. Non va sommato.

## IOM DTM, HDX, INFORM, confini

46. **DTM**:
    - la finestra filtra la data del **report**, non quella dell'evento;
    - `idp_stock_dtm` = somma su motivi e province d'origine (gender=all), all'ultima data di ogni unità;
    - `idp_stock_natural_disaster_dtm` tiene le righe il cui motivo contiene "Natural disaster".
47. **HDX HAPI, finestra**: si tengono i record il cui periodo si sovrappone alla finestra. Se non c'è nulla, si prende il rilascio più recente finito prima (`quality_flag=fallback_older_release`).
48. **HDX bisogni**:
    - le categorie vengono divise in gruppo/genere/età, e l'originale resta in `value_text`;
    - `population_status=all` diventa `pop_hno`;
    - nelle colonne c'è il totale "Intersectoral";
    - le righe "Households" hanno unità households.
49. **HDX altri dataset**:
    - rifugiati = gruppi REF+ASY+OIP, ospitati (asilo = paese) o all'estero (origine = paese);
    - rientrati = RET (rifugiati) e RDP (sfollati interni);
    - gli IDP di HAPI hanno il flag `duplicate_of_iom_dtm`.
50. **Prezzi**: media dell'anno per ogni bene. Un codice di mercato che non è nei confini viene abbinato per nome dentro l'admin1, poi per le coordinate del mercato.
51. **INFORM**:
    - serie ricalcolata: si prende l'edizione più recente (metodologia attuale) e, al suo interno, il workflow con `GNAYear` = anno;
    - valore = score 0–10 per gli indicatori aggregati, valore grezzo per quelli raw;
    - validità = `ValidityYear` dell'API, altrimenti `reference_year` del catalogo, altrimenti l'anno dei dati;
    - flag `recalculated_series`: in un nowcast c'è rischio di leakage.
52. **Confini**:
    - OCHA COD via FieldMaps; si leggono tutti i record del paese;
    - admin0 fuso, con pcode = ISO3; admin1/2 senza pcode doppi; EPSG:4326.

## ReliefWeb e Montandon

53. **ReliefWeb**:
    - serve un GLIDE: prima quello di EM-DAT, poi GDACS/card, con e senza suffisso ISO;
    - il disaster trovato viene confrontato con l'evento (tipo, paese, data entro 7 giorni) e riceve un verdetto MATCH/PROBABLE/MISMATCH/NOT FOUND;
    - la finestra è di `days_after` giorni interi; il certificato TLS non viene verificato.
54. **`disp_mention`**: regex che cerca numeri di almeno 3 cifre (o con separatori) entro 4 parole prima o 3 dopo una parola di spostamento (italiano, inglese, spagnolo). Gli anni sono esclusi; l'unità è households se compare "famil/household/hogares". Flag `regex_unverified`.
55. **Montandon**:
    - impatti cercati per corr_id (esatto, per fonte) e per finestra (paese + hazard, da −1 a +30 giorni);
    - quelli trovati solo per finestra hanno `window_only` e non entrano nelle colonne wide;
    - doppioni (collezione, categoria, tipo, valore) eliminati;
    - eventi collegati entro ±2 giorni, repliche entro 14 giorni e 150 km;
    - mai fuso con le altre fonti.

## Tabelle wide (`dataset/<ISO3>/<YEAR>/`)

56. **Admin**: per ogni unità, l'ultimo valore valido entro il 31/12 dell'anno, anche se vecchio (`columns.csv` dice quanto) e anche se successivo all'evento. Per i valori pre-evento c'è `access.as_of`.
    - Se più righe finiscono lo stesso giorno vince la prima, quindi il risultato dipende dall'ordine.
    - Le unità fuori dai confini vengono scartate.
57. **Evento, episodio, location**: per ogni colonna vince il valore con `known_at` più recente; quelli senza `known_at` contano come i più vecchi.
    - Montandon: vince l'impatto con la data più recente, non il più alto.
    - ShakeMap: colonne `_last` dell'ultima versione, più `shakemap_n_versions`.
58. **Anno di un evento**: inizio GDACS, altrimenti la prima data IDMC.

## Possibili difetti trovati leggendo il codice (non corretti)

- `extract_variables.fetch_event` passa `output_root` solo a `get_event_data` e non a `event_dir_from_eventdata`: con una cartella diversa da `output/` i file finiscono in due posti.
- `build()` scrive sempre `dataset_variables.xlsx` nella cartella corrente, anche con un `dataset_root` diverso.
- `dataset/gdacs.extract_event`: con 0 notizie EMM, `min(dates)` su una lista vuota fa fallire la build dell'evento.
- La docstring di `resolve_ghsl_tif` dice "coarsest", ma il codice prende la risoluzione più fine.
- Il controllo "rural plus urban = pop_ghsl" di `test_pipeline.ipynb` può fallire per la popolazione in celle d'acqua o senza SMOD (punto 35).
- La griglia ripete le celle delle aree sovrapposte: sommare tutta la griglia di un episodio conta due volte (punto 37).
