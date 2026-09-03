"""Справочник фото товаров: ключ → имя файла на Wikimedia Commons.

Только справочник. Начального наполнения каталога (категории, товары,
зоны, промокоды) здесь нет и не будет: данные живут в базе, а новый
магазин наполняется переносом или руками через админку — генератор
демо-данных в боевом коде означал бы риск однажды затереть настоящий
каталог.

Ключи — те же `image_key`, что хранятся в товаре. Нет ключа в таблице —
карточка покажет эмодзи вместо фотографии, это допустимый запасной
вариант, а не отказ.
"""

from __future__ import annotations

__all__ = ["PHOTOS", "photo_url"]

PHOTOS: dict[str, str] = {
    "avocado": "Avocado_Hass_-_single_and_halved.jpg",
    "mango": "Mango_-_single.jpg",
    "banana": "Bananas.jpg",
    "apple_green": "Granny_Smith_Apples.jpg",
    "mandarin": "Mandarin_Oranges_(Citrus_Reticulata).jpg",
    "pomegranate": "Pomegranate_(fruit).jpg",
    "strawberry": "Fresh_strawberries.jpg",
    "blueberry": "Blueberries.jpg",
    "raspberry": "Raspberries_(Rubus_Idaeus).jpg",
    "cherry_tomato": "Cherry_Tomato_on_Vine.JPG",
    "cucumber": "Cucumber_picture.jpg",
    "potato": "Potatoes.jpg",
    "carrot": "Carrots.JPG",
    "bell_pepper": "Red_Bell_Pepper.jpg",
    "dill": "Fresh_Dill_Leaves.JPG",
    "cilantro": "Coriander_fresh.JPG",
    "lettuce": "Iceberg_lettuce_in_SB.jpg",
    "cashew": "Cashews.jpg",
    "dried_apricot": "Abricot_sec.jpg",
    "walnut": "Shelled_English_Walnuts_2331px.jpg",
    "bread": "Dark_rye_bread.JPG",
    "croissant": "Croissant.jpg",
    "orange_juice": "Orange_juice_1_edit1.jpg",
    "water": "Water_bottle_blue.jpg",
    "rice": "1121-Sella-Basmati-Rice.jpg",
    "honey": "Honey_(Italian-miele)_in_a_jar.jpg",
    "beef": "Beef_tenderloin_picture.jpg",
    "lamb": "Lamb_meat_(1).jpg",
    "grapes": "Thompson_seedless_grapes.JPG",
    "oat_cookies": "Oatmeal-Cookie.jpg",
    "pork": "Fresh_meat.jpg",
    "chicken": "Raw_chicken.jpg",
    "pickles": "Pickled_cucumbers_in_a_jar.jpg",
    "sweets": "Chocolate_(blue_background).jpg",
    "frozen": "Frozen_berries.jpg",
    "sausage": "Salami_aka.jpg",
}


def photo_url(key: str | None, width: int | None = None) -> str | None:
    """URL картинки через редирект-сервис Wikimedia — не зависим от
    MD5-пути их внутреннего хранилища."""
    from urllib.parse import quote

    file = PHOTOS.get(key) if key else None
    if not file:
        return None
    # safe="!~*'()" — эти символы `encodeURIComponent` в JS тоже не
    # экранирует (в отличие от `quote()` по умолчанию), а часть имён
    # файлов на Wikimedia содержит круглые скобки буквально:
    # «Honey_(Italian-miele)_in_a_jar.jpg».
    url = ("https://commons.wikimedia.org/wiki/Special:FilePath/"
          + quote(file, safe="!~*'()"))
    return f"{url}?width={width}" if width else url
