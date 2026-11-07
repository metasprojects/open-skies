"""Zoning rules: what kind of place every real building is, from the best evidence available.

Priority (most specific first, as KnoxMap and other OSM-based generators do it):
  1. the building's own tag (Overture buildings.class / subtype, from OSM building=*),
  2. the land-use area it stands in (Overture base/land_use, from OSM landuse=*),
  3. ESA WorldCover at its spot (built-up / cropland / ...),
  4. density: lots of tall neighbours makes an untagged building commercial (a city centre).

Zones are small ints so they can be rasterised and stored compactly.
"""
RESIDENTIAL, COMMERCIAL, INDUSTRIAL, AGRICULTURAL, MILITARY, PORT, CIVIC, AIRPORT, UNKNOWN = range(9)
NAMES = ["residential", "commercial", "industrial", "agricultural", "military", "port", "civic", "airport", "unknown"]
COLOURS = [(230, 200, 60), (235, 90, 70), (150, 110, 200), (120, 190, 90), (90, 120, 60), (60, 160, 220),
           (240, 150, 200), (200, 200, 200), (170, 170, 170)]

# Overture building class (OSM building=*) -> zone. Generic "yes"/None fall through to land use.
BUILDING_CLASS = {
    # homes
    "residential": RESIDENTIAL, "house": RESIDENTIAL, "detached": RESIDENTIAL, "semidetached_house": RESIDENTIAL,
    "terrace": RESIDENTIAL, "apartments": RESIDENTIAL, "dormitory": RESIDENTIAL, "bungalow": RESIDENTIAL,
    "hut": RESIDENTIAL, "static_caravan": RESIDENTIAL, "houseboat": RESIDENTIAL,
    # business
    "commercial": COMMERCIAL, "office": COMMERCIAL, "retail": COMMERCIAL, "supermarket": COMMERCIAL,
    "kiosk": COMMERCIAL, "hotel": COMMERCIAL, "parking": COMMERCIAL, "bank": COMMERCIAL,
    # industry
    "industrial": INDUSTRIAL, "warehouse": INDUSTRIAL, "factory": INDUSTRIAL, "manufacture": INDUSTRIAL,
    "storage_tank": INDUSTRIAL, "silo": INDUSTRIAL, "service": INDUSTRIAL, "transformer_tower": INDUSTRIAL,
    "digester": INDUSTRIAL, "gasometer": INDUSTRIAL,
    # farms
    "farm": AGRICULTURAL, "farm_auxiliary": AGRICULTURAL, "barn": AGRICULTURAL, "cowshed": AGRICULTURAL,
    "greenhouse": AGRICULTURAL, "stable": AGRICULTURAL, "sty": AGRICULTURAL, "shed": AGRICULTURAL,
    "glasshouse": AGRICULTURAL, "slurry_tank": AGRICULTURAL,
    # military / aviation
    "military": MILITARY, "bunker": MILITARY, "barracks": MILITARY,
    "hangar": AIRPORT,
    # public
    "school": CIVIC, "university": CIVIC, "college": CIVIC, "kindergarten": CIVIC, "hospital": CIVIC,
    "civic": CIVIC, "government": CIVIC, "public": CIVIC, "church": CIVIC, "temple": CIVIC, "mosque": CIVIC,
    "religious": CIVIC, "shrine": CIVIC, "cathedral": CIVIC, "chapel": CIVIC, "fire_station": CIVIC,
    "train_station": CIVIC, "transportation": CIVIC, "stadium": CIVIC, "sports_hall": CIVIC, "grandstand": CIVIC,
}

# Overture land_use subtype/class (OSM landuse=*, military=*, aeroway=*) -> zone.
LAND_USE = {
    "residential": RESIDENTIAL, "developed": RESIDENTIAL,
    "commercial": COMMERCIAL, "retail": COMMERCIAL,
    "industrial": INDUSTRIAL, "quarry": INDUSTRIAL, "landfill": INDUSTRIAL, "brownfield": INDUSTRIAL,
    "construction": INDUSTRIAL, "railway": INDUSTRIAL, "works": INDUSTRIAL,
    "farmland": AGRICULTURAL, "farmyard": AGRICULTURAL, "agriculture": AGRICULTURAL, "orchard": AGRICULTURAL,
    "vineyard": AGRICULTURAL, "greenhouse_horticulture": AGRICULTURAL, "aquaculture": AGRICULTURAL,
    "plant_nursery": AGRICULTURAL, "meadow": AGRICULTURAL, "animal_keeping": AGRICULTURAL,
    "military": MILITARY, "base": MILITARY, "barracks": MILITARY, "naval_base": MILITARY, "airfield": MILITARY,
    "danger_area": MILITARY, "range": MILITARY, "training_area": MILITARY,
    "port": PORT, "harbour": PORT, "marina": PORT,
    "education": CIVIC, "school": CIVIC, "university": CIVIC, "college": CIVIC, "hospital": CIVIC,
    "religious": CIVIC, "cemetery": CIVIC, "institutional": CIVIC,
    "aerodrome": AIRPORT, "airport": AIRPORT,
}

# Overture place basic_category (real businesses and facilities, ~500k on the Taiwan map) -> zone.
# A building takes the majority zone of the places inside it; this is what tells a shop-house
# street from a housing estate, which footprints alone cannot.
PLACE_INDUSTRIAL = {"manufacturer", "supplier_or_distributor", "b2b_transportation_and_storage_service", "b2b_service",
                    "shipping_or_delivery_service", "building_or_construction_service", "printing_service",
                    "industrial_equipment", "factory", "warehouse", "wholesaler", "energy_company", "utility_service",
                    "recycling_center", "scrap_metal_yard", "industrial_company"}
PLACE_CIVIC = {"religious_organization", "christian_place_of_worship", "buddhist_place_of_worship", "place_of_learning",
               "education", "specialty_school", "preschool", "elementary_school", "high_school", "college_university",
               "educational_service", "hospital", "health_care", "specialized_health_care", "medical_service", "surgery",
               "government_office", "community_and_government", "police_station", "fire_station", "library",
               "community_center", "museum", "train_station", "stadium_arena", "civic_organization",
               "public_transit_facility_or_service", "senior_living_facility", "post_office", "courthouse"}
PLACE_AGRICULTURAL = {"farm", "agricultural_service", "ranch", "orchard", "vineyard", "fishery"}
PLACE_MILITARY = {"military_base", "military", "army_base", "naval_base", "air_force_base"}
PLACE_SKIP = {None, "", "park", "mountain", "campground", "recreational_trail_or_path", "bridge", "historic_site",
              "food_truck_stand", "atm", "parking", "beach", "lake", "river", "island", "landmark_and_historical_building"}


def place_zone(category):
    if category in PLACE_SKIP:
        return None
    if category in PLACE_INDUSTRIAL:
        return INDUSTRIAL
    if category in PLACE_CIVIC:
        return CIVIC
    if category in PLACE_AGRICULTURAL:
        return AGRICULTURAL
    if category in PLACE_MILITARY:
        return MILITARY
    return COMMERCIAL   # shops, food, services, offices, hotels, banks, venues: the rest is business


# Order in which overlapping land-use areas win when rasterised (later wins).
LAND_USE_PRIORITY = [AGRICULTURAL, RESIDENTIAL, CIVIC, COMMERCIAL, INDUSTRIAL, PORT, AIRPORT, MILITARY]

# ESA WorldCover class -> zone for buildings nobody tagged and no land-use area covers.
WORLDCOVER = {50: RESIDENTIAL, 40: AGRICULTURAL, 30: AGRICULTURAL, 20: AGRICULTURAL, 10: AGRICULTURAL}


def building_zone(cls, subtype):
    for key in (cls, subtype):
        if key and key in BUILDING_CLASS:
            return BUILDING_CLASS[key]
    return None


def land_use_zone(subtype, cls):
    for key in (cls, subtype):
        if key and key in LAND_USE:
            return LAND_USE[key]
    return None
