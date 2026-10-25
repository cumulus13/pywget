# pywget

Command line wget downloader alternative.

## Installing

Install and update using [pip](https://pip.pypa.io/en/stable/quickstart/):

```bash
pip install pywget
```

`pywget` supports Python 2 and newer, Python 3 and newer, and PyPy.

## Example

What does it look like? Here is an example of a simple `pywget` program:

```python
from pywget import wget

link = "http://www.test.com/test.tar.gz"
wget.download(link, "/home/root/Downloads")
```

Or run on terminal:

```bash
pywget "http://www.test.com/test.tar.gz" -p /home/root/Downloads
```

## Support

* Python 2.7+, Python 3.x
* Windows, Linux

## License
[LICENSE](LICENSE)

## 👤 Author
        
[Hadi Cahyadi](mailto:cumulus13@gmail.com)
    

[![Buy Me a Coffee](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://www.buymeacoffee.com/cumulus13)

[![Donate via Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/cumulus13)
 
[Support me on Patreon](https://www.patreon.com/cumulus13)