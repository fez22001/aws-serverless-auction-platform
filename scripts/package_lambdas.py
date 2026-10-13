"""Reproducible Lambda packages. AWS Python runtime supplies boto3; offline only."""
import argparse
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]

def package(output):
    output=Path(output); output.mkdir(parents=True,exist_ok=True)
    paths=[]
    for folder in sorted((ROOT/'hw10/lambdas').iterdir()):
        if not (folder/'lambda_function.py').is_file(): continue
        path=output/f'{folder.name}.zip'
        with zipfile.ZipFile(path,'w') as archive:
            for source,name in [(folder/'lambda_function.py','lambda_function.py'),(ROOT/'hw10/shared/auction_common.py','auction_common.py')]:
                entry=zipfile.ZipInfo(name,date_time=(2020,1,1,0,0,0))
                entry.create_system=3; entry.external_attr=0o100644 << 16
                # Stored entries are stable across zlib and host platform versions.
                entry.compress_type=zipfile.ZIP_STORED
                archive.writestr(entry,source.read_bytes())
        paths.append(path)
    return paths

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=ROOT/'.local/lambda-zips')
    for path in package(parser.parse_args().output): print(path)
