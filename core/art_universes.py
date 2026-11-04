"""Artistic scene transformations, separate from legacy colour harmonisations.

The historical colour keys remain in ``universes.py`` for saved-book
compatibility. These ``art_`` keys request a newly illustrated thematic scene
from the artistic service before the normal colouring segmentation begins.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ArtUniverse:
    key: str
    label: str
    description: str
    prompt: str
    accent: str = "#a48a58"


ART_DIRECTION = """Create ONE original, refined COLORED INK DRAWING from the
supplied photograph for a premium adult colouring gift book. Apply the selected
world through a genuinely new illustrated setting, coherent thematic scenery,
and carefully chosen accessories. A change of colour grading alone is not an
application of this world. Replacing and enriching the background is explicitly
allowed and expected. Preserve the placement, relative scale and main pose of
the original subjects so the photograph remains clearly recognizable.

Preserve every actual subject: people, pets and meaningful objects. Keep the
same number of principal people and animals. For every person retain the exact
recognizable identity, facial proportions, natural asymmetry, actual age,
expression, gaze, skin tone, hairstyle, body proportions and hand anatomy.
Do not enlarge eyes, make a chibi or caricature version, beautify or age a face,
change ethnicity, change pose or replace a person. Keep faces unobstructed.
For pets preserve species, coat markings, facial features and main pose.
Retain the original clothing unless the selected world explicitly suggests a
small optional outer garment or accessory; such additions must remain secondary
and must not alter the person's silhouette, hide the face or erase distinctive
objects from the photograph. Do not add new identifiable people.

The result must clearly be a professional illustrator's DRAWING with elegant,
clean, continuous black pen contours and approximately 24–32 harmonious flat
colour families. Draw eyelids, eyebrows, nose, lips, ears, grouped hair and fabric
folds deliberately to preserve likeness. Use clean closed shapes and medium
detail that an adult can physically colour. On skin use one natural base and at
most two restrained, clearly bounded shadow shapes. Organize background detail
into meaningful, comfortably colourable shapes; avoid fields of tiny dots,
individual hair strands and microscopic ornamental cells.

No photographic texture, pore noise, automatic posterization, pixel mosaic,
low-poly facets, watercolour, oil painting, paintbrush texture or photorealistic
gradients. Lighting must be expressed through restrained flat shadow shapes.
Invent an original world: no recognizable franchise characters, branded
costumes, copied protected designs, artist imitation, logos, readable text,
numbers, watermark, comparison panel, page mockup or added page border.
Return only the single finished illustration in portrait orientation.
"""


_HISTORICAL = (
    ArtUniverse(
        "art_royal", "Royal / Château",
        "Votre souvenir dans une cour royale, entre château, arches et détails de conte.",
        """Build an elegant royal castle setting: a grand stone arch, a refined
        palace courtyard or reception room, patterned floor and restrained
        decorative banners without lettering. Use a coherent courtly world,
        rather than just a golden tint. An optional simple cape over the existing
        clothing or a small regal accessory is allowed, but preserve the original
        outfit's important details and keep the head and face completely clear.
        Make the source subjects the unmistakable centre of the scene.""",
        "#a78953",
    ),
    ArtUniverse(
        "art_floral", "Floral / Romantique",
        "Une scène romantique dessinée, avec rosiers, pergola et feuillages délicats.",
        """Reimagine the background as a romantic rose garden with a graceful
        pergola, climbing roses, broad leaves and a curved garden path. Group
        petals into readable, colourable flower heads. Let the original subjects
        remain central and recognizable, wearing their original clothes. Arrange
        the floral scenery around the subjects without covering faces or hands;
        do not merely recolour the original background pink.""",
        "#bd919c",
    ),
    ArtUniverse(
        "art_noel", "Noël / Hiver",
        "Un souvenir de fête dans un décor hivernal, avec sapins, lanternes et chalet.",
        """Create a welcoming winter celebration around a snow-covered chalet
        or festive village square, with fir trees, a few large ornaments and
        lanterns. Make snow and branches clean shapes rather than granular
        photographic texture. A small optional scarf over existing clothing is
        allowed. Preserve the people's faces, ages and pose, and keep natural
        skin tones. Use an actual illustrated winter scene, not a blue tint.""",
        "#7e9b96",
    ),
)


_NEW = (
    ArtUniverse(
        "art_fairy", "Forêt enchantée",
        "Arbres majestueux, champignons et lanternes composent une forêt de conte.",
        """Place the original subjects in an enchanted woodland clearing with
        an arch of large tree roots, broad ferns, a few oversized mushrooms and
        hanging lanterns. Suggest gentle magic with a handful of clearly outlined
        motifs, never clouds of glitter or glowing photographic haze. Keep all
        people human and all pets their original species; do not add pointed
        ears, wings or fantasy anatomy to the source subjects.""",
        "#7c987c",
    ),
    ArtUniverse(
        "art_celestial", "Rêverie céleste",
        "Lunes, constellations et arches astrales ouvrent un décor poétique et étoilé.",
        """Create a poetic celestial garden with a crescent-moon arch, a
        stylized celestial globe or orrery, clearly grouped constellation motifs
        and broad sculpted clouds. This is an allegorical star-lit setting, not
        a technological space mission. Use a limited number of readable stars
        and large negative spaces, without printed star names or thousands of
        speckles. Preserve the actual subjects and their original clothing.""",
        "#8a87af",
    ),
    ArtUniverse(
        "art_ocean_deep", "Merveilles sous-marines",
        "Coraux, poissons et jardins aquatiques entourent un pavillon sous l’océan.",
        """Build an original undersea world viewed from an airy glass garden
        pavilion, with large coral fans, sea plants, shells and a few distinctive
        fish outside. Keep the source subjects comfortably posed in the pavilion,
        with unobstructed recognizable faces and their original bodies. Do not
        turn a person into a mermaid or a pet into a sea creature. Draw the glass
        structure with sparse clean contours, never reflections across a face.""",
        "#6d9da9",
    ),
    ArtUniverse(
        "art_safari", "Safari et jungle",
        "Une aventure entre végétation tropicale, acacias et animaux en arrière-plan.",
        """Reimagine the setting as a lush safari outlook where broad jungle
        leaves open onto a savannah with acacia trees. Add at most a few clearly
        separated background animals, such as a giraffe or elephant, drawn at a
        smaller scale than the original subjects. Keep original pets unchanged
        and visually distinct from the scenery's wildlife. An optional small
        explorer bag is allowed; do not cover faces with hats or transform poses.""",
        "#9a9a66",
    ),
    ArtUniverse(
        "art_adventure", "Cités oubliées",
        "Ruines végétalisées, escaliers de pierre et objets d’exploration racontent l’aventure.",
        """Create an adventurous lost-city setting with vine-covered stone
        terraces, a distant temple-like ruin of original design, broad stairs
        and an old stone gateway. Add a simple closed explorer satchel and a
        compass motif if they fit without changing the source pose. Do not put
        writing, maps with readable labels or real cultural insignia in the
        scene. Keep faces, hands and important original objects clearly visible.""",
        "#a28e6d",
    ),
    ArtUniverse(
        "art_steampunk", "Atelier steampunk",
        "Engrenages, verrières et machines imaginaires créent un atelier rétrofuturiste.",
        """Design a retrofuturist clockwork workshop with an arched glass roof,
        grouped brass gears, large pipes and one fanciful original machine.
        Make gears bold and readable instead of a mesh of tiny teeth. A small
        optional waistcoat over existing clothes or goggles hanging from a belt
        is allowed; never place goggles on the face or conceal the eyes. Maintain
        the source people's anatomy, recognizable age and original main pose.""",
        "#9a7e62",
    ),
    ArtUniverse(
        "art_artnouveau", "Arabesques Art nouveau",
        "Courbes végétales, vitraux et ferronneries dessinent un écrin élégant.",
        """Build an original Art nouveau architectural interior or garden
        entrance: flowing wrought-iron curves, stylized irises, broad stained-glass
        sections and graceful botanical arches integrated into the setting.
        Use the movement's general design language without imitating a named
        artist or reproducing a known poster. The source subjects stay fully
        recognizable; ornaments must not become an added page border or intrude
        into facial features.""",
        "#8c9e87",
    ),
    ArtUniverse(
        "art_japanese", "Jardin japonais",
        "Pont de bois, lanternes et cerisiers composent un jardin calme et dessiné.",
        """Create a serene Japanese-inspired garden with a small arched wooden
        bridge, carefully spaced rocks, a paper lantern or stone lantern,
        cherry-blossom branches and a koi pond. Keep branches and water ripples
        as clean grouped drawing shapes. Preserve the source person's ethnicity,
        face, hairstyle and original clothing; the change is in the setting,
        not in the person's identity. Include no writing or copied shrine logos.""",
        "#b08f9d",
    ),
    ArtUniverse(
        "art_mediterranean", "Été méditerranéen",
        "Village ensoleillé, citronniers et terrasse sur la mer enrichissent le souvenir.",
        """Place the scene on a welcoming Mediterranean village terrace with
        terracotta pots, a lemon tree, broad bougainvillea clusters, pale stone
        steps and a glimpse of the sea between buildings. Use clean inked
        architecture and controlled flat sunlight shapes. Keep subjects in their
        original recognizable pose and clothes; do not tan or otherwise change
        their skin tone. The setting must be visibly redesigned, not just warmed.""",
        "#c19c72",
    ),
    ArtUniverse(
        "art_fantasy", "Légendes fantastiques",
        "Citadelle de montagne, passerelles et créatures lointaines ouvrent une aventure de fantasy.",
        """Invent an outdoor medieval-fantasy realm with a mountain citadel,
        a carved stone bridge, a dramatic but simply drawn rocky landscape and,
        if space permits, one small distant original dragon silhouette. Keep the
        adventure setting distinct from a royal court. Do not copy a franchise's
        armour, architecture, creatures or symbols. An optional simple travel
        cloak may sit over existing clothing, leaving faces and hands unobscured.""",
        "#87919d",
    ),
    ArtUniverse(
        "art_winterpalace", "Palais de glace",
        "Arches cristallines, grandes rosaces et sculptures de glace forment un palais imaginaire.",
        """Create an original crystalline winter palace with generous ice
        arches, carved geometric balustrades and a few large snowflake-like
        architectural rosettes. Use elegant ink contours and flat translucent-
        looking colour groups, not glossy photographic ice or fragmented facets.
        This is an architectural fairy-tale world without Christmas decorations
        or any recognizable franchise costume. Keep people's features and natural
        skin tones intact; their faces must not become pale blue or crystalline.""",
        "#9bb4bf",
    ),
    ArtUniverse(
        "art_masquerade", "Carnaval de Venise",
        "Canal, palais vénitiens et étoffes de fête créent une scène de carnaval.",
        """Reimagine the background as an original Venetian carnival scene:
        a quiet canal, an ornate small bridge, palace facades, draped fabric and
        a few decorative carnival masks displayed on stands or in the scenery.
        Do not put masks over the source people's faces. An optional modest
        festive shoulder accessory is allowed while retaining the original
        clothing's important features. Avoid new people, brand marks or text.""",
        "#ad8e9d",
    ),
    ArtUniverse(
        "art_circus", "Cirque des rêves",
        "Chapiteau, carrousel et lanternes évoquent un cirque poétique et chaleureux.",
        """Create a welcoming dream-circus setting with a striped tent canopy,
        an original wooden carousel detail, large ribbon arcs and a few hanging
        lanterns. Keep it elegant and suitable for an adult gift-book drawing,
        not a cartoon poster. Preserve the source subjects' actual pose; do not
        make them perform acrobatics, change anatomy or dress them as clowns.
        Include no frightening faces, new human performers or readable signs.""",
        "#bd9684",
    ),
    ArtUniverse(
        "art_space_explorer", "Explorateurs de l’espace",
        "Dôme spatial, rover et planètes lointaines transportent la scène sur un autre monde.",
        """Design an original optimistic space-exploration outpost with a
        panoramic habitat window or open observatory, a simple rover, a distant
        spacecraft and two clearly drawn planets above a broad alien landscape.
        Make it a technological exploration world, distinct from a poetic
        celestial garden. A light optional exploration jacket is allowed, but
        no helmet or visor may cover a recognizable face. Do not copy existing
        fictional ships, uniforms, logos or printed instrument labels.""",
        "#879cb0",
    ),
    ArtUniverse(
        "art_bohogarden", "Jardin bohème",
        "Tissages, coussins, pots et niches végétales composent un refuge artisanal.",
        """Build a richly illustrated bohemian courtyard with a woven hanging
        chair, a few broad textile patterns, clay planters, leafy arched niches
        and carefully spaced cushions. Emphasize craft, furniture and tactile
        objects drawn as clean flat shapes, not the rose pergola of a romantic
        flower garden. Keep all original people and pets in the same recognizable
        main pose, without forcing them into a new seated position or adding
        jewellery, tattoos or face decorations.""",
        "#a4957b",
    ),
)


ART_UNIVERSES: dict[str, ArtUniverse] = {item.key: item for item in (*_HISTORICAL, *_NEW)}
NEW_UNIVERSE_KEYS: tuple[str, ...] = tuple(item.key for item in _NEW)


def is_art_universe(key: object) -> bool:
    """Return whether a key requests scene illustration rather than a palette."""
    return isinstance(key, str) and key in ART_UNIVERSES


def art_universe_catalog() -> list[dict[str, object]]:
    """Public metadata only: no image-service configuration or prompt payload."""
    return [
        {"key": item.key, "label": item.label, "description": item.description,
         "kind": "artistic", "accent": item.accent, "is_new": item.key in NEW_UNIVERSE_KEYS}
        for item in ART_UNIVERSES.values()
    ]


def build_art_prompt(key: str) -> str:
    """Build a complete illustration prompt without photo-only contradictions."""
    if not is_art_universe(key):
        raise ValueError("Choisis un univers artistique disponible.")
    universe = ART_UNIVERSES[key]
    return ART_DIRECTION + "\nSELECTED ARTISTIC WORLD:\n" + universe.prompt.strip()
