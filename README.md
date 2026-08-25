# Tau Till

Cash register software for my parents' grocery store in Kazakhstan. It replaces
the proprietary till they had been renting.


## Running it

```
python3 server.py
```

Then open `http://127.0.0.1:8000`. Python 3.8 or newer, no dependencies. Without
a thermal printer the receipt fails loudly and the sale is still recorded.

To build the Windows bundle:

```
python3 pack/build.py
```

It downloads CPython and the UCRT.
