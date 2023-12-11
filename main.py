import glob, subprocess, os

# conf
PROJECT_FOLDER_PATH = 'D:/RESEARCH/Bitbucket/SOURCE-atlassian-bitbucket-8.16.0/app'

# conf default settings
VINEFLOWER_PATH = './jar/vineflower-1.9.3.jar'
JAVA_PATH = 'D:/tools/jdk/jdk-11.0.19/bin/java.exe'
ALL_JARS_REGEX = '/**/*.jar'
ALL_CLASSES_REGEX = '/**/*.class'


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

if __name__ == '__main__':
    jar_files = glob.glob(PROJECT_FOLDER_PATH + ALL_JARS_REGEX, recursive=True)
    class_files = glob.glob(PROJECT_FOLDER_PATH + ALL_CLASSES_REGEX, recursive=True)
    
    decompileJars(jar_files)
    decompileClasses(class_files)

    
