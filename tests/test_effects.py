from deepyugioh.cards.effects import parse_script

SCRIPT = """
local s,id=GetID()
function s.initial_effect(c)
	--Special Summon itself from the GY
	local e1=Effect.CreateEffect(c)
	e1:SetDescription(aux.Stringid(id,0))
	e1:SetCategory(CATEGORY_SPECIAL_SUMMON+CATEGORY_TOGRAVE)
	e1:SetType(EFFECT_TYPE_SINGLE+EFFECT_TYPE_TRIGGER_O)
	e1:SetProperty(EFFECT_FLAG_DELAY)
	e1:SetCode(EVENT_TO_GRAVE)
	e1:SetCountLimit(1,id)
	c:RegisterEffect(e1)
	local e2=e1:Clone()
	e2:SetCode(EVENT_REMOVE)
	c:RegisterEffect(e2)
end
"""


def test_parse_script_extracts_effect_structure():
    e1, e2 = parse_script(SCRIPT)
    assert e1.categories == ["CATEGORY_SPECIAL_SUMMON", "CATEGORY_TOGRAVE"]
    assert e1.types == ["EFFECT_TYPE_SINGLE", "EFFECT_TYPE_TRIGGER_O"]
    assert e1.event == ["EVENT_TO_GRAVE"]
    assert e1.properties == ["EFFECT_FLAG_DELAY"]
    assert e1.count_limit == "1,id"
    # clones inherit everything and override what they set themselves
    assert e2.categories == e1.categories
    assert e2.event == ["EVENT_REMOVE"]
