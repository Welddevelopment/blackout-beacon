#!/usr/bin/env python3
"""Blackout Beacon — London-wide area briefing (boroughs + neighbourhoods).

Extends the single South Kensington area card to full London coverage using
the SAME Gemini structured-output pipeline as briefing.py:

  * WAVE 1 (--wave 1): one card per London borough (32 + City of London).
  * WAVE 2 (--wave 2): neighbourhood-level cards for the most populous /
    central boroughs (e.g. Camden Town, Brixton, Shoreditch, Stratford).

Serving-layer contract (llm.py routes area cards by question keywords):
  * id MUST be "local-help-points-<slug>", category "local", lang "en".
  * keywords MUST include the area name, its major neighbourhoods /
    landmarks / stations, and postcode prefixes ("sw7", "nw1", ...).
    Required keywords are merged in client-side after generation, so the
    routing contract holds even if the model drops some.

This script is strictly ADDITIVE: it only ever writes
cards/local-help-points-<slug>.json files and refreshes cards/meta.json.
It never deletes or rewrites any other card.
"""

import argparse
import json
import math
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import briefing
from briefing import CARDS_DIR, Card, discover_model, generate_cards, load_api_key

AREA_PREFIX = "local-help-points-"
BACKOFF_SECONDS = 10

# --------------------------------------------------------------------------
# System instruction — London-wide variant of briefing.SYSTEM_INSTRUCTION.
# Installed into the briefing module at runtime so generate_cards() (which
# reads briefing.SYSTEM_INSTRUCTION) briefs for all of London, not just
# South Kensington. briefing.py itself is left untouched.
# --------------------------------------------------------------------------

AREA_SYSTEM_INSTRUCTION = """\
You are a meticulous emergency-preparedness briefing officer compiling
offline "area help point" readiness cards for residents of Greater London,
UK — one card per borough or neighbourhood — to be served by a small
offline model during a prolonged power/network outage.

Non-negotiable rules — ACCURACY OVER CREATIVITY, CONFIDENCE OVER HEDGING:
1. Use only real, current UK emergency information: 999 and 112
   (emergency), NHS 111 (urgent medical advice), 105 (power cuts),
   0800 111 999 (gas emergency). Never invent phone numbers.
2. Name only real, major NHS hospitals and urgent treatment centres, with
   their real street addresses. If you are not confident a hospital's A&E
   is 24-hour and currently open, OMIT it — do not hedge item by item.
3. Write like a knowledgeable local giving directions, not a disclaimer
   sheet: real road names, cross-streets, rough distances, bus routes.
   Include approximate travel guidance for each hospital where you are
   confident of the rough geography (e.g. "from Brixton station: ~25 min
   walk south along Effra Road, or bus 250 towards Croydon"). If unsure
   of a route, give the address only — never invent roads or bus numbers.
4. The phrase "(verify locally)" must appear AT MOST ONCE per card, only
   as a single closing note. NEVER attach it to individual items. Banned
   phrasing anywhere in content: "check for information", "consult local
   resources", "look for updates", "seek local advice" or similar
   deflections — this beacon IS the information point.
5. Hazard notes (rivers, flood zones) must reflect real geography — the
   Thames and London's tributary rivers (Lea, Wandle, Brent, Ravensbourne,
   Crane, Roding, ...). If unsure, omit. Prefer omission over speculation.
6. Content must be practical and stepwise: short numbered sections, plain
   words, readable by a stressed person on a phone screen. 200-400 words
   per card.
7. Keywords: lowercase retrieval keywords including the area name, its
   major neighbourhoods / stations / landmarks, and postcode prefixes
   exactly as given in the prompt (e.g. "sw7", "nw1").
"""

CONTENT_SPEC = """\
For EVERY card:
- category "local", lang "en", content 200-400 words covering, in order:
  1. Nearest hospitals with 24-hour A&E serving the area, each with its
     street address AND approximate travel guidance from a major local
     station or landmark — pattern: "from <station/landmark>: ~<N> min
     walk along <road>, or bus <route>" — with real road names and rough
     distances, wherever you are confident of the geography. Note which
     is a Major Trauma Centre if applicable. If unsure of a route, give
     the address only.
  2. NHS urgent treatment centres / walk-in centres in or near the area
     (real, named ones only, with address and, where confident, the same
     kind of travel guidance).
  3. Area-specific hazard notes: rivers, flood-risk zones, and any other
     notable local risk. Only real geography; if unsure, omit.
  4. One short line: pharmacies and larger supermarkets in the area's
     main shopping streets (name the streets) can supply water, first-aid
     items and medicines during outages.
- Start content with the reminder that life-threatening emergencies are
  always 999 first.
- End content with EXACTLY ONE closing note, e.g. "Note: addresses and
  opening hours may have changed — confirm on arrival where possible."
  This is the ONLY hedge allowed on the card; no per-item "(verify
  locally)" tags, no "check local information" phrasing anywhere.
- keywords: lowercase; MUST include the area name, the neighbourhoods /
  stations listed for it below, and every postcode prefix listed for it.
"""

# --------------------------------------------------------------------------
# WAVE 1 — the 32 London boroughs + City of London.
# places/postcodes seed both the prompt and the client-side keyword merge.
# --------------------------------------------------------------------------

BOROUGHS: List[dict] = [
    {"slug": "barking-dagenham", "name": "Barking and Dagenham",
     "places": ["barking", "dagenham", "becontree", "chadwell heath"],
     "postcodes": ["ig11", "rm8", "rm9", "rm10"],
     "hazards": "Thames-side (Barking Riverside); River Roding flood zones"},
    {"slug": "barnet", "name": "Barnet",
     "places": ["finchley", "hendon", "golders green", "edgware", "mill hill", "high barnet"],
     "postcodes": ["n2", "n3", "n12", "n20", "nw4", "nw7", "nw11", "en5"],
     "hazards": "Dollis Brook / Silk Stream surface-water flood risk"},
    {"slug": "bexley", "name": "Bexley",
     "places": ["bexleyheath", "sidcup", "erith", "welling", "crayford", "thamesmead"],
     "postcodes": ["da5", "da6", "da7", "da8", "da14", "da15", "da16", "se2"],
     "hazards": "Thames-side (Erith); Rivers Cray and Shuttle flood zones"},
    {"slug": "brent", "name": "Brent",
     "places": ["wembley", "kilburn", "willesden", "harlesden", "neasden", "kingsbury"],
     "postcodes": ["nw2", "nw9", "nw10", "ha0", "ha9"],
     "hazards": "River Brent and Wealdstone Brook flood zones; Brent Reservoir"},
    {"slug": "bromley", "name": "Bromley",
     "places": ["bromley", "orpington", "beckenham", "penge", "chislehurst", "biggin hill"],
     "postcodes": ["br1", "br2", "br3", "br4", "br5", "br6", "br7", "se20"],
     "hazards": "River Ravensbourne headwaters flood risk"},
    {"slug": "camden", "name": "Camden",
     "places": ["camden town", "hampstead", "kentish town", "kings cross", "holborn", "bloomsbury", "belsize park"],
     "postcodes": ["nw1", "nw3", "nw5", "wc1", "n1c", "n19"],
     "hazards": "Hidden River Fleet; surface-water flooding (Hampstead 1975 storm precedent)"},
    {"slug": "city-of-london", "name": "City of London",
     "places": ["bank", "barbican", "st pauls", "liverpool street", "moorgate"],
     "postcodes": ["ec1", "ec2", "ec3", "ec4"],
     "hazards": "Thames-side; dense high-rise district, lift outages in power cuts"},
    {"slug": "croydon", "name": "Croydon",
     "places": ["croydon", "purley", "coulsdon", "thornton heath", "south norwood", "crystal palace"],
     "postcodes": ["cr0", "cr2", "cr4", "cr5", "cr7", "cr8", "se19", "se25"],
     "hazards": "River Wandle sources; Purley/Kenley surface-water flood risk"},
    {"slug": "ealing", "name": "Ealing",
     "places": ["ealing", "acton", "southall", "greenford", "hanwell", "perivale"],
     "postcodes": ["w3", "w5", "w7", "w13", "ub1", "ub2", "ub5", "ub6"],
     "hazards": "River Brent flood zones (Hanwell, Greenford); Grand Union Canal"},
    {"slug": "enfield", "name": "Enfield",
     "places": ["enfield town", "edmonton", "palmers green", "southgate", "winchmore hill", "ponders end"],
     "postcodes": ["en1", "en2", "en3", "n9", "n13", "n14", "n18", "n21"],
     "hazards": "River Lea / Lee Navigation flood zones; Salmons and Pymmes Brooks"},
    {"slug": "greenwich", "name": "Greenwich",
     "places": ["greenwich", "woolwich", "eltham", "charlton", "plumstead", "thamesmead"],
     "postcodes": ["se3", "se7", "se9", "se10", "se18", "se28"],
     "hazards": "Thames-side (Thames Barrier at Charlton); Thamesmead low-lying"},
    {"slug": "hackney", "name": "Hackney",
     "places": ["hackney", "shoreditch", "dalston", "stoke newington", "homerton", "clapton", "hoxton"],
     "postcodes": ["e5", "e8", "e9", "n16", "n1"],
     "hazards": "River Lea flood zones (Hackney Wick, Lea Bridge); Hackney Marshes"},
    {"slug": "hammersmith-fulham", "name": "Hammersmith and Fulham",
     "places": ["hammersmith", "fulham", "shepherds bush", "white city", "parsons green"],
     "postcodes": ["w6", "w12", "w14", "sw6"],
     "hazards": "Thames-side; low-lying Fulham riverside flood risk"},
    {"slug": "haringey", "name": "Haringey",
     "places": ["tottenham", "wood green", "hornsey", "crouch end", "muswell hill", "highgate"],
     "postcodes": ["n4", "n8", "n10", "n15", "n17", "n22"],
     "hazards": "River Lea (Tottenham Hale); Moselle Brook surface-water flooding"},
    {"slug": "harrow", "name": "Harrow",
     "places": ["harrow", "wealdstone", "pinner", "stanmore", "harrow on the hill"],
     "postcodes": ["ha1", "ha2", "ha3", "ha5", "ha7"],
     "hazards": "Wealdstone Brook and River Pinn surface-water flood risk"},
    {"slug": "havering", "name": "Havering",
     "places": ["romford", "hornchurch", "upminster", "rainham", "collier row"],
     "postcodes": ["rm1", "rm2", "rm3", "rm5", "rm7", "rm11", "rm12", "rm13", "rm14"],
     "hazards": "Thames-side (Rainham Marshes); Rivers Rom, Beam and Ingrebourne"},
    {"slug": "hillingdon", "name": "Hillingdon",
     "places": ["uxbridge", "hayes", "ruislip", "west drayton", "northwood", "heathrow"],
     "postcodes": ["ub3", "ub4", "ub7", "ub8", "ub9", "ub10", "ha4", "ha6"],
     "hazards": "Rivers Colne, Pinn and Frays flood zones; Grand Union Canal"},
    {"slug": "hounslow", "name": "Hounslow",
     "places": ["hounslow", "chiswick", "brentford", "feltham", "isleworth", "osterley"],
     "postcodes": ["tw3", "tw4", "tw5", "tw7", "tw8", "tw13", "tw14", "w4"],
     "hazards": "Thames-side (Chiswick, Isleworth tidal); Rivers Crane and Brent"},
    {"slug": "islington", "name": "Islington",
     "places": ["islington", "angel", "highbury", "holloway", "archway", "finsbury park", "clerkenwell"],
     "postcodes": ["n1", "n5", "n7", "n19", "ec1"],
     "hazards": "Hidden rivers; localised surface-water flash flooding"},
    {"slug": "kensington-chelsea", "name": "Kensington and Chelsea",
     "places": ["kensington", "chelsea", "notting hill", "south kensington", "earls court", "ladbroke grove", "north kensington"],
     "postcodes": ["sw3", "sw5", "sw7", "sw10", "w8", "w10", "w11"],
     "hazards": "Thames-side (Chelsea Embankment); basement-flat flood risk"},
    {"slug": "kingston", "name": "Kingston upon Thames",
     "places": ["kingston upon thames", "new malden", "surbiton", "chessington", "norbiton"],
     "postcodes": ["kt1", "kt2", "kt3", "kt5", "kt6", "kt9"],
     "hazards": "Thames-side (non-tidal reach); Hogsmill River flood zones"},
    {"slug": "lambeth", "name": "Lambeth",
     "places": ["brixton", "clapham", "streatham", "vauxhall", "kennington", "stockwell", "west norwood", "waterloo"],
     "postcodes": ["sw2", "sw4", "sw8", "sw9", "sw16", "se11", "se24", "se27"],
     "hazards": "Thames-side (Albert Embankment); hidden River Effra surface flooding"},
    {"slug": "lewisham", "name": "Lewisham",
     "places": ["lewisham", "deptford", "catford", "brockley", "forest hill", "sydenham", "new cross"],
     "postcodes": ["se4", "se6", "se8", "se12", "se13", "se14", "se23", "se26"],
     "hazards": "Rivers Ravensbourne, Quaggy and Pool flood zones; Deptford Creek tidal"},
    {"slug": "merton", "name": "Merton",
     "places": ["wimbledon", "mitcham", "morden", "colliers wood", "raynes park"],
     "postcodes": ["sw19", "sw20", "cr4", "sm4"],
     "hazards": "River Wandle flood zones (Colliers Wood, Mitcham)"},
    {"slug": "newham", "name": "Newham",
     "places": ["stratford", "east ham", "west ham", "plaistow", "canning town", "forest gate", "beckton"],
     "postcodes": ["e6", "e7", "e12", "e13", "e15", "e16"],
     "hazards": "Thames-side (Royal Docks); River Lea flood zones; low-lying Beckton"},
    {"slug": "redbridge", "name": "Redbridge",
     "places": ["ilford", "wanstead", "woodford", "barkingside", "gants hill"],
     "postcodes": ["ig1", "ig2", "ig3", "ig4", "ig5", "ig6", "ig8", "e11", "e18"],
     "hazards": "River Roding flood zones (Ilford, Woodford)"},
    {"slug": "richmond", "name": "Richmond upon Thames",
     "places": ["richmond", "twickenham", "teddington", "barnes", "kew", "hampton", "mortlake"],
     "postcodes": ["tw1", "tw2", "tw9", "tw10", "tw11", "tw12", "sw13", "sw14"],
     "hazards": "Thames-side both banks; tidal flooding of riverside roads (e.g. Richmond towpath)"},
    {"slug": "southwark", "name": "Southwark",
     "places": ["peckham", "camberwell", "bermondsey", "dulwich", "rotherhithe", "elephant and castle", "borough", "walworth"],
     "postcodes": ["se1", "se5", "se15", "se16", "se17", "se21", "se22"],
     "hazards": "Thames-side (Bankside, Rotherhithe); hidden rivers surface flooding"},
    {"slug": "sutton", "name": "Sutton",
     "places": ["sutton", "carshalton", "wallington", "cheam", "worcester park"],
     "postcodes": ["sm1", "sm2", "sm3", "sm5", "sm6"],
     "hazards": "River Wandle sources (Carshalton); Pyl Brook surface flooding"},
    {"slug": "tower-hamlets", "name": "Tower Hamlets",
     "places": ["whitechapel", "bethnal green", "bow", "poplar", "canary wharf", "mile end", "stepney", "isle of dogs"],
     "postcodes": ["e1", "e2", "e3", "e14"],
     "hazards": "Thames-side (Isle of Dogs low-lying); River Lea; dense high-rise, lift outages"},
    {"slug": "waltham-forest", "name": "Waltham Forest",
     "places": ["walthamstow", "leyton", "leytonstone", "chingford", "highams park"],
     "postcodes": ["e4", "e10", "e11", "e17"],
     "hazards": "River Lea flood zones (Leyton, Walthamstow Marshes); reservoirs chain"},
    {"slug": "wandsworth", "name": "Wandsworth",
     "places": ["battersea", "putney", "tooting", "balham", "wandsworth town", "earlsfield"],
     "postcodes": ["sw11", "sw12", "sw15", "sw17", "sw18"],
     "hazards": "Thames-side; River Wandle mouth flood zones (Earlsfield)"},
    {"slug": "westminster", "name": "Westminster",
     "places": ["westminster", "paddington", "marylebone", "mayfair", "soho", "pimlico", "victoria", "st johns wood", "maida vale"],
     "postcodes": ["w1", "w2", "w9", "nw1", "nw8", "sw1"],
     "hazards": "Thames-side (Millbank, Pimlico low-lying); dense central district"},
]

# --------------------------------------------------------------------------
# WAVE 2 — neighbourhood cards for the most populous / central boroughs.
# 15 boroughs x 3-4 neighbourhoods = 50 cards (the cap).
# "stations" are tube/rail names — required keywords per the contract.
# --------------------------------------------------------------------------

NEIGHBOURHOODS: List[dict] = [
    # Camden (4)
    {"slug": "camden-town", "name": "Camden Town", "borough": "Camden",
     "stations": ["camden town", "camden road", "chalk farm", "mornington crescent"],
     "postcodes": ["nw1"]},
    {"slug": "kings-cross", "name": "King's Cross", "borough": "Camden",
     "stations": ["kings cross st pancras", "st pancras international", "euston"],
     "postcodes": ["n1c", "wc1", "nw1"]},
    {"slug": "hampstead", "name": "Hampstead", "borough": "Camden",
     "stations": ["hampstead", "hampstead heath", "belsize park", "finchley road"],
     "postcodes": ["nw3"]},
    {"slug": "kentish-town", "name": "Kentish Town", "borough": "Camden",
     "stations": ["kentish town", "kentish town west", "tufnell park", "gospel oak"],
     "postcodes": ["nw5"]},
    # Westminster (4)
    {"slug": "paddington", "name": "Paddington", "borough": "Westminster",
     "stations": ["paddington", "edgware road", "lancaster gate", "bayswater"],
     "postcodes": ["w2"]},
    {"slug": "soho", "name": "Soho", "borough": "Westminster",
     "stations": ["oxford circus", "tottenham court road", "piccadilly circus", "leicester square"],
     "postcodes": ["w1"]},
    {"slug": "pimlico", "name": "Pimlico", "borough": "Westminster",
     "stations": ["pimlico", "victoria", "sloane square"],
     "postcodes": ["sw1"]},
    {"slug": "marylebone", "name": "Marylebone", "borough": "Westminster",
     "stations": ["marylebone", "baker street", "regents park", "bond street"],
     "postcodes": ["w1", "nw1"]},
    # Hackney (4)
    {"slug": "shoreditch", "name": "Shoreditch", "borough": "Hackney",
     "stations": ["shoreditch high street", "old street", "hoxton", "liverpool street"],
     "postcodes": ["e1", "e2", "ec2"]},
    {"slug": "dalston", "name": "Dalston", "borough": "Hackney",
     "stations": ["dalston junction", "dalston kingsland", "haggerston"],
     "postcodes": ["e8"]},
    {"slug": "stoke-newington", "name": "Stoke Newington", "borough": "Hackney",
     "stations": ["stoke newington", "rectory road", "stamford hill"],
     "postcodes": ["n16"]},
    {"slug": "homerton", "name": "Homerton", "borough": "Hackney",
     "stations": ["homerton", "hackney central", "hackney wick"],
     "postcodes": ["e9"]},
    # Lambeth (4)
    {"slug": "brixton", "name": "Brixton", "borough": "Lambeth",
     "stations": ["brixton", "loughborough junction", "herne hill"],
     "postcodes": ["sw2", "sw9"]},
    {"slug": "clapham", "name": "Clapham", "borough": "Lambeth",
     "stations": ["clapham common", "clapham north", "clapham south", "clapham junction"],
     "postcodes": ["sw4", "sw11"]},
    {"slug": "streatham", "name": "Streatham", "borough": "Lambeth",
     "stations": ["streatham", "streatham hill", "streatham common"],
     "postcodes": ["sw16"]},
    {"slug": "vauxhall", "name": "Vauxhall", "borough": "Lambeth",
     "stations": ["vauxhall", "oval", "nine elms"],
     "postcodes": ["sw8", "se11"]},
    # Newham (4)
    {"slug": "stratford", "name": "Stratford", "borough": "Newham",
     "stations": ["stratford", "stratford international", "maryland", "west ham"],
     "postcodes": ["e15", "e20"]},
    {"slug": "east-ham", "name": "East Ham", "borough": "Newham",
     "stations": ["east ham", "upton park", "beckton"],
     "postcodes": ["e6"]},
    {"slug": "canning-town", "name": "Canning Town", "borough": "Newham",
     "stations": ["canning town", "custom house", "royal victoria"],
     "postcodes": ["e16"]},
    {"slug": "forest-gate", "name": "Forest Gate", "borough": "Newham",
     "stations": ["forest gate", "wanstead park", "manor park"],
     "postcodes": ["e7"]},
    # Tower Hamlets (3)
    {"slug": "whitechapel", "name": "Whitechapel", "borough": "Tower Hamlets",
     "stations": ["whitechapel", "aldgate east", "shadwell", "stepney green"],
     "postcodes": ["e1"]},
    {"slug": "canary-wharf", "name": "Canary Wharf", "borough": "Tower Hamlets",
     "stations": ["canary wharf", "west india quay", "heron quays", "poplar"],
     "postcodes": ["e14"]},
    {"slug": "bethnal-green", "name": "Bethnal Green", "borough": "Tower Hamlets",
     "stations": ["bethnal green", "cambridge heath", "mile end"],
     "postcodes": ["e2"]},
    # Southwark (3)
    {"slug": "peckham", "name": "Peckham", "borough": "Southwark",
     "stations": ["peckham rye", "queens road peckham", "nunhead"],
     "postcodes": ["se15"]},
    {"slug": "bermondsey", "name": "Bermondsey", "borough": "Southwark",
     "stations": ["bermondsey", "south bermondsey", "london bridge", "rotherhithe"],
     "postcodes": ["se16", "se1"]},
    {"slug": "camberwell", "name": "Camberwell", "borough": "Southwark",
     "stations": ["denmark hill", "loughborough junction"],
     "postcodes": ["se5"]},
    # Islington (3)
    {"slug": "angel", "name": "Angel", "borough": "Islington",
     "stations": ["angel", "essex road", "highbury and islington"],
     "postcodes": ["n1"]},
    {"slug": "holloway", "name": "Holloway", "borough": "Islington",
     "stations": ["holloway road", "caledonian road", "archway"],
     "postcodes": ["n7", "n19"]},
    {"slug": "finsbury-park", "name": "Finsbury Park", "borough": "Islington",
     "stations": ["finsbury park", "manor house", "arsenal"],
     "postcodes": ["n4"]},
    # Kensington and Chelsea (3)
    {"slug": "notting-hill", "name": "Notting Hill", "borough": "Kensington and Chelsea",
     "stations": ["notting hill gate", "ladbroke grove", "westbourne park"],
     "postcodes": ["w11", "w10"]},
    {"slug": "earls-court", "name": "Earl's Court", "borough": "Kensington and Chelsea",
     "stations": ["earls court", "west brompton", "gloucester road"],
     "postcodes": ["sw5"]},
    {"slug": "chelsea", "name": "Chelsea", "borough": "Kensington and Chelsea",
     "stations": ["sloane square", "south kensington", "imperial wharf"],
     "postcodes": ["sw3", "sw10"]},
    # Wandsworth (3)
    {"slug": "battersea", "name": "Battersea", "borough": "Wandsworth",
     "stations": ["battersea power station", "battersea park", "clapham junction"],
     "postcodes": ["sw11", "sw8"]},
    {"slug": "putney", "name": "Putney", "borough": "Wandsworth",
     "stations": ["putney", "putney bridge", "east putney"],
     "postcodes": ["sw15"]},
    {"slug": "tooting", "name": "Tooting", "borough": "Wandsworth",
     "stations": ["tooting broadway", "tooting bec", "tooting"],
     "postcodes": ["sw17"]},
    # Haringey (3)
    {"slug": "tottenham", "name": "Tottenham", "borough": "Haringey",
     "stations": ["tottenham hale", "seven sisters", "white hart lane", "bruce grove"],
     "postcodes": ["n15", "n17"]},
    {"slug": "wood-green", "name": "Wood Green", "borough": "Haringey",
     "stations": ["wood green", "alexandra palace", "turnpike lane", "bounds green"],
     "postcodes": ["n22"]},
    {"slug": "crouch-end", "name": "Crouch End", "borough": "Haringey",
     "stations": ["hornsey", "harringay", "highgate", "archway"],
     "postcodes": ["n8"]},
    # Lewisham (3)
    {"slug": "deptford", "name": "Deptford", "borough": "Lewisham",
     "stations": ["deptford", "deptford bridge", "greenwich"],
     "postcodes": ["se8"]},
    {"slug": "catford", "name": "Catford", "borough": "Lewisham",
     "stations": ["catford", "catford bridge", "bellingham"],
     "postcodes": ["se6"]},
    {"slug": "new-cross", "name": "New Cross", "borough": "Lewisham",
     "stations": ["new cross", "new cross gate"],
     "postcodes": ["se14"]},
    # Croydon (3)
    {"slug": "crystal-palace", "name": "Crystal Palace", "borough": "Croydon",
     "stations": ["crystal palace", "gipsy hill", "anerley"],
     "postcodes": ["se19", "se20"]},
    {"slug": "purley", "name": "Purley", "borough": "Croydon",
     "stations": ["purley", "purley oaks", "reedham"],
     "postcodes": ["cr8"]},
    {"slug": "thornton-heath", "name": "Thornton Heath", "borough": "Croydon",
     "stations": ["thornton heath", "norbury", "selhurst"],
     "postcodes": ["cr7"]},
    # Ealing (3)
    {"slug": "acton", "name": "Acton", "borough": "Ealing",
     "stations": ["acton town", "east acton", "acton central", "acton main line"],
     "postcodes": ["w3"]},
    {"slug": "southall", "name": "Southall", "borough": "Ealing",
     "stations": ["southall", "hanwell"],
     "postcodes": ["ub1", "ub2"]},
    {"slug": "ealing-broadway", "name": "Ealing Broadway", "borough": "Ealing",
     "stations": ["ealing broadway", "ealing common", "west ealing"],
     "postcodes": ["w5", "w13"]},
    # Brent (3)
    {"slug": "wembley", "name": "Wembley", "borough": "Brent",
     "stations": ["wembley park", "wembley central", "wembley stadium"],
     "postcodes": ["ha9", "ha0"]},
    {"slug": "kilburn", "name": "Kilburn", "borough": "Brent",
     "stations": ["kilburn", "kilburn park", "brondesbury", "west hampstead"],
     "postcodes": ["nw6"]},
    {"slug": "willesden", "name": "Willesden", "borough": "Brent",
     "stations": ["willesden green", "willesden junction", "dollis hill", "harlesden"],
     "postcodes": ["nw10", "nw2"]},
]

WAVE2_CAP = 50
assert len(NEIGHBOURHOODS) <= WAVE2_CAP, "wave 2 exceeds the 50-card cap"


# --------------------------------------------------------------------------
# Prompt builders
# --------------------------------------------------------------------------

def borough_prompt(specs: List[dict]) -> str:
    lines = [
        f"Generate one local 'help points' readiness card for EACH of these "
        f"{len(specs)} London boroughs — EXACTLY {len(specs)} cards, ids "
        f"copied verbatim.\n",
        CONTENT_SPEC,
        "Cards to produce:",
    ]
    for i, s in enumerate(specs, 1):
        lines.append(
            f"{i}. id \"{AREA_PREFIX}{s['slug']}\" — London Borough of {s['name']}.\n"
            f"   Title like \"{s['name']} Emergency Help Points\".\n"
            f"   Neighbourhoods: {', '.join(s['places'])}.\n"
            f"   Postcode prefixes (each must be its own keyword): {', '.join(s['postcodes'])}.\n"
            f"   Hazard hint (verify, refine or omit if unsure): {s['hazards']}."
        )
    return "\n".join(lines)


def neighbourhood_prompt(specs: List[dict]) -> str:
    lines = [
        f"Generate one local 'help points' readiness card for EACH of these "
        f"{len(specs)} London neighbourhoods — EXACTLY {len(specs)} cards, "
        f"ids copied verbatim. Content must be specific to the neighbourhood "
        f"(nearest A&E may be in an adjacent borough — that is fine).\n",
        CONTENT_SPEC,
        "Cards to produce:",
    ]
    for i, s in enumerate(specs, 1):
        lines.append(
            f"{i}. id \"{AREA_PREFIX}{s['slug']}\" — {s['name']} "
            f"(borough of {s['borough']}).\n"
            f"   Title like \"{s['name']} Emergency Help Points\".\n"
            f"   Tube/rail stations (each must be its own keyword): {', '.join(s['stations'])}.\n"
            f"   Postcode prefixes (each must be its own keyword): {', '.join(s['postcodes'])}."
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Generation helpers
# --------------------------------------------------------------------------

def chunk_even(items: List[dict], max_size: int) -> List[List[dict]]:
    """Split into near-equal batches no larger than max_size."""
    n_batches = max(1, math.ceil(len(items) / max_size))
    base, extra = divmod(len(items), n_batches)
    out, start = [], 0
    for i in range(n_batches):
        size = base + (1 if i < extra else 0)
        out.append(items[start:start + size])
        start += size
    return out


def call_with_backoff(client, model_id: str, prompt: str) -> Optional[List[Card]]:
    """generate_cards + one 10s-backoff retry (for rate limits / flakes)."""
    try:
        return generate_cards(client, model_id, prompt)
    except Exception as exc:
        print(f"[areas] call failed ({type(exc).__name__}); "
              f"backing off {BACKOFF_SECONDS}s and retrying once ...")
        time.sleep(BACKOFF_SECONDS)
        try:
            return generate_cards(client, model_id, prompt)
        except Exception as exc2:
            print(f"[areas] call failed again ({type(exc2).__name__}); giving up on this call")
            return None


def required_keywords(spec: dict) -> List[str]:
    kws = [spec["name"].lower()]
    kws += [p.lower() for p in spec.get("places", [])]
    kws += [s.lower() for s in spec.get("stations", [])]
    if spec.get("borough"):
        kws.append(spec["borough"].lower())
    kws += [p.lower() for p in spec["postcodes"]]
    return kws


def enforce_contract(card: Card, spec: dict) -> Card:
    """Pin id/category/lang and merge required routing keywords client-side."""
    card.id = AREA_PREFIX + spec["slug"]
    card.category = "local"
    card.lang = "en"
    merged = [k.lower().strip() for k in card.keywords if k.strip()]
    have = set(merged)
    for kw in required_keywords(spec):
        if kw not in have:
            merged.append(kw)
            have.add(kw)
    card.keywords = merged
    return card


# Style contract (user feedback from live testing): cards must read like a
# knowledgeable local, not a disclaimer sheet. At most ONE closing verify
# note; no deflecting "go check elsewhere" phrasing — the beacon IS the
# information point.
BANNED_PHRASES = (
    "check for information", "consult local resources", "look for updates",
    "seek local advice", "check local information", "consult local",
)

STYLE_RETRY_SUFFIX = (
    "\n\nGenerate ONLY this single card. STYLE REMINDER: the phrase "
    "'(verify locally)' may appear AT MOST ONCE, as a single closing note; "
    "never after individual items. No 'check/consult/look for' deflections. "
    "Include concrete travel directions (roads, minutes, bus routes) for "
    "each hospital where you are confident of the geography."
)


def style_problems(card: Card) -> List[str]:
    text = card.content.lower()
    probs = []
    n = text.count("(verify locally)")
    if n > 1:
        probs.append(f'"(verify locally)" x{n}')
    for p in BANNED_PHRASES:
        if p in text:
            probs.append(f'banned phrase "{p}"')
    return probs


def match_spec(card: Card, specs: List[dict]) -> Optional[dict]:
    for s in specs:
        if card.id == AREA_PREFIX + s["slug"]:
            return s
    for s in specs:  # tolerate id drift: slug substring or name-in-title
        cid = card.id.lower()
        if s["slug"] in cid or s["name"].lower() in card.title.lower():
            return s
    return None


def run_wave(client, model_id: str, specs: List[dict], prompt_fn,
             batch_size: int = 7) -> Tuple[List[Card], List[str]]:
    """Generate one card per spec: batched, then per-area retries for gaps."""
    got: Dict[str, Card] = {}
    batches = chunk_even(specs, batch_size)
    for bi, batch in enumerate(batches, 1):
        want = ", ".join(s["slug"] for s in batch)
        print(f"[areas] batch {bi}/{len(batches)} ({len(batch)} areas): {want}")
        cards = call_with_backoff(client, model_id, prompt_fn(batch))
        for card in cards or []:
            spec = match_spec(card, batch)
            if spec is None:
                print(f"[areas]   ignoring unexpected card id: {card.id}")
                continue
            if spec["slug"] in got:
                continue
            card = enforce_contract(card, spec)
            words = len(card.content.split())
            print(f"  {card.id:<42} {words:>4}w  {card.title[:44]}")
            got[spec["slug"]] = card

    # Style pass: hedge-heavy or deflecting cards get one focused retry.
    for spec in specs:
        slug = spec["slug"]
        if slug not in got:
            continue
        probs = style_problems(got[slug])
        if not probs:
            continue
        print(f"[areas] style check '{slug}': {'; '.join(probs)} — regenerating once ...")
        cards = call_with_backoff(client, model_id,
                                  prompt_fn([spec]) + STYLE_RETRY_SUFFIX)
        for card in cards or []:
            if match_spec(card, [spec]):
                card = enforce_contract(card, spec)
                if not style_problems(card):
                    words = len(card.content.split())
                    print(f"  {card.id:<42} {words:>4}w  {card.title[:44]} (style fixed)")
                    got[slug] = card
                else:
                    print(f"[areas]   retry still off-style; keeping best effort")
                break

    missing = [s for s in specs if s["slug"] not in got]
    for spec in missing:
        print(f"[areas] '{spec['slug']}' missing after batches; single-card retry ...")
        cards = call_with_backoff(
            client, model_id,
            prompt_fn([spec]) + "\n\nGenerate ONLY this single card.",
        )
        for card in cards or []:
            if match_spec(card, [spec]):
                card = enforce_contract(card, spec)
                words = len(card.content.split())
                print(f"  {card.id:<42} {words:>4}w  {card.title[:44]}")
                got[spec["slug"]] = card
                break

    failed = [s["slug"] for s in specs if s["slug"] not in got]
    ordered = [got[s["slug"]] for s in specs if s["slug"] in got]
    return ordered, failed


# --------------------------------------------------------------------------
# Writing, meta, validation — strictly additive
# --------------------------------------------------------------------------

def write_area_cards(cards: List[Card]) -> None:
    CARDS_DIR.mkdir(exist_ok=True)
    for card in cards:
        payload = dict(card.model_dump(), source="gemini")
        path = CARDS_DIR / f"{card.id}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")


def update_meta() -> dict:
    """Refresh meta.json in place: keep existing keys, update coverage facts."""
    meta_path = CARDS_DIR / "meta.json"
    meta = {}
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    card_files = [p for p in CARDS_DIR.glob("*.json") if p.name != "meta.json"]
    area_files = [p for p in card_files if p.name.startswith("local-help-points")]
    meta["location"] = "London, UK — borough & neighbourhood coverage"
    meta["briefed_at"] = datetime.now(timezone.utc).isoformat()
    meta["card_count"] = len(card_files)
    meta["area_cards"] = len(area_files)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    return meta


def validate_area_files() -> int:
    """Re-read every area card file and re-validate against the Card schema."""
    n = 0
    for path in sorted(CARDS_DIR.glob("local-help-points*.json")):
        Card.model_validate(json.loads(path.read_text(encoding="utf-8")))
        n += 1
    json.loads((CARDS_DIR / "meta.json").read_text(encoding="utf-8"))
    return n


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="London area-card briefing")
    parser.add_argument("--wave", type=int, choices=(1, 2), required=True,
                        help="1 = boroughs, 2 = neighbourhoods")
    args = parser.parse_args()

    api_key = load_api_key()
    if not api_key:
        print("[areas] GEMINI_API_KEY not found (env or .env) — cannot brief areas.")
        return 2

    from google import genai

    client = genai.Client(api_key=api_key)
    model_id = discover_model(client)
    # Steer generate_cards() London-wide: it reads briefing.SYSTEM_INSTRUCTION
    # at call time, so patching the module attribute is enough.
    briefing.SYSTEM_INSTRUCTION = AREA_SYSTEM_INSTRUCTION

    if args.wave == 1:
        label, specs, prompt_fn = "wave 1 — 33 borough cards", BOROUGHS, borough_prompt
    else:
        label, specs, prompt_fn = (f"wave 2 — {len(NEIGHBOURHOODS)} neighbourhood cards",
                                   NEIGHBOURHOODS, neighbourhood_prompt)

    print("=" * 78)
    print(f"  Blackout Beacon — London area briefing, {label}")
    print(f"  model: {model_id}")
    print("=" * 78)

    cards, failed = run_wave(client, model_id, specs, prompt_fn)
    write_area_cards(cards)
    meta = update_meta()
    n_valid = validate_area_files()

    print("-" * 78)
    print(f"[areas] wave {args.wave} complete: {len(cards)}/{len(specs)} cards written")
    print(f"[areas] validated {n_valid} area card files against the Card schema")
    print(f"[areas] meta.json: card_count={meta['card_count']} "
          f"area_cards={meta['area_cards']} location={meta['location']!r}")
    if failed:
        print(f"[areas] FAILED areas ({len(failed)}): {', '.join(failed)}")
        return 1
    print("[areas] no failures")
    return 0


if __name__ == "__main__":
    sys.exit(main())
