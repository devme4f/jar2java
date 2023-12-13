import glob, subprocess, os
from bs4 import BeautifulSoup 

# conf
PROJECT_FOLDER_PATH = 'D:/RESEARCH/Bitbucket-8.16.0/SOURCE-atlassian-bitbucket-8.16.0/app'

# conf default settings
VINEFLOWER_PATH = './jar/vineflower-1.9.3.jar'
JAVA_PATH = 'D:/tools/jdk/jdk-11.0.19/bin/java.exe'
ALL_JARS_REGEX = '/**/*.jar'
ALL_CLASSES_REGEX = '/**/*.class'
ALL_XML_REGEX = '/**/*.xml'


def decompile(jav_file, out_jav_folder):
    command = f"{JAVA_PATH} -jar {VINEFLOWER_PATH} {jav_file} --folder {out_jav_folder}"
    print(f'[STATUS] - decompiling: {jav_file}')
    try:
        subprocess.run(command, check=True, shell=True)
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] - Error decompile: {jav_file}")
        return
    
    print(f"[SUCCESS] - decompiled, deleting jav file: {jav_file}")
    os.remove(jav_file)

def getReady(jar_file):
    jar_folder = os.path.splitext(os.path.basename(jar_file))[0]
    out_jar_folder = os.path.dirname(jar_file) + '/' + jar_folder
    out_jar_folder = out_jar_folder.replace('\\', '/')
    jar_file = jar_file.replace('\\', '/')
    return jar_file, out_jar_folder

def decompileJars(jar_files):
    print(f'[STATUS] - decompiling {len(jar_files)} jar files')

    for jar_file in jar_files:
        jar_file, out_jar_folder = getReady(jar_file)
        decompile(jar_file, out_jar_folder)

def decompileClasses(class_files):
    print(f'[STATUS] - decompiling {len(class_files)} class files')
    for class_file in class_files:
        class_file, out_class_folder = getReady(class_file)
        out_class_folder = os.path.dirname(out_class_folder)
        decompile(class_file, out_class_folder)

# beautify xml file, resource file in jar after decompile may minified
def beautifyXML(xml_files):
    print(f'[STATUS] - Found {len(xml_files)} xml files')
    for xml_file in xml_files:
        xml_file, out_xml_folder = getReady(xml_file)

        try:
            with open(file=xml_file, mode='r', encoding='utf-8') as f:
                text_lines = f.readlines()
                lines = len(text_lines)
            if lines < 10:
                print(f'[STATUS] - Beautifying: {xml_file}')
                text = ''.join(text_lines)
                temp = BeautifulSoup(text, "xml") 
                new_xml = temp.prettify()
                with open(file=xml_file, mode='w', encoding='utf-8') as f:
                    f.write(new_xml)
        except Exception as e:
            print(f'[ERROR] Error beautifying: {xml_file}')

if __name__ == '__main__':
    jar_files = glob.glob(PROJECT_FOLDER_PATH + ALL_JARS_REGEX, recursive=True)
    class_files = glob.glob(PROJECT_FOLDER_PATH + ALL_CLASSES_REGEX, recursive=True)
    xml_files = glob.glob(PROJECT_FOLDER_PATH + ALL_XML_REGEX, recursive=True)

    decompileJars(jar_files)
    decompileClasses(class_files)
    beautifyXML(xml_files)


    
